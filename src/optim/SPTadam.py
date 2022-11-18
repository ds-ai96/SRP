# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import logging
import math
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, List

import torch
import torch.distributed as dist
import torch.optim
from fairseq.dataclass import FairseqDataclass
from fairseq.optim import FairseqOptimizer, register_optimizer
from fairseq.optim.fused_adam import get_fused_adam_class
from omegaconf import II, OmegaConf


logger = logging.getLogger(__name__)


@dataclass
class SPTAdamConfig(FairseqDataclass):
    adam_betas: Any = field(
        default=(0.9, 0.999), metadata={"help": "betas for Adam optimizer"}
    )
    adam_eps: float = field(
        default=1e-8, metadata={"help": "epsilon for Adam optimizer"}
    )
    weight_decay: float = field(default=0.0, metadata={"help": "weight decay"})
    use_old_adam: bool = field(
        default=False, metadata={"help": "Use fairseq.optim.adam.Adam"}
    )
    fp16_adam_stats: bool = field(
        default=False, metadata={"help": "use FP16 stats (with automatic scaling)"}
    )
    # TODO common vars below in parent
    tpu: bool = II("common.tpu")
    lr: List[float] = II("optimization.lr")


@register_optimizer("spt_adam", dataclass=SPTAdamConfig)
class SPTAdam(FairseqOptimizer):
    """Adam optimizer for fairseq.

    Important note: this optimizer corresponds to the "AdamW" variant of
    Adam in its weight decay behavior. As such, it is most closely
    analogous to torch.optim.AdamW from PyTorch.
    """

    def __init__(self, cfg: SPTAdamConfig, params):
        super().__init__(cfg)
        fused_adam_cls = get_fused_adam_class()
        use_fused_adam = (
            not getattr(cfg, "use_old_adam", False)
            and fused_adam_cls is not None
            and torch.cuda.is_available()
        )
        if getattr(cfg, "tpu", False):
            if self.cfg.fp16_adam_stats:
                raise NotImplementedError("--fp16-adam-stats is only supported on GPU")
            # on TPUs we use the Adam defined here, since it
            # automatically casts gradients to FP32
            self._optimizer = Adam(params, **self.optimizer_config)
        elif use_fused_adam:
            logger.info("using FusedAdam")
            self._optimizer = fused_adam_cls(
                params, use_fp16_stats=self.cfg.fp16_adam_stats, **self.optimizer_config
            )
        else:
            if self.cfg.fp16_adam_stats:
                raise NotImplementedError(
                    "--fp16-adam-stats is only supported with FusedAdamV1"
                )
            self._optimizer = Adam(params, **self.optimizer_config)

    @property
    def optimizer_config(self):
        """
        Return a kwarg dictionary that will be used to override optimizer
        args stored in checkpoints. This allows us to load a checkpoint and
        resume training using a different set of optimizer args, e.g., with a
        different learning rate.
        """
        return {
            "lr": self.cfg.lr[0]
            if isinstance(self.cfg.lr, Collection)
            else self.cfg.lr,
            "betas": eval(self.cfg.adam_betas)
            if isinstance(self.cfg.adam_betas, str)
            else OmegaConf.to_container(self.cfg.adam_betas),
            "eps": self.cfg.adam_eps,
            "weight_decay": self.cfg.weight_decay,
        }

    def average_params(self):
        """Reduce Params is only used during BMUF distributed training."""
        state_dict = self.optimizer.state_dict()
        total_gpus = float(dist.get_world_size())

        for _, value in state_dict["state"].items():
            value["exp_avg"] /= total_gpus
            value["exp_avg_sq"] /= total_gpus
            dist.all_reduce(value["exp_avg"], op=dist.ReduceOp.SUM)
            dist.all_reduce(value["exp_avg_sq"], op=dist.ReduceOp.SUM)


class Adam(torch.optim.Optimizer):
    r"""Implements Adam algorithm.

    This implementation is modified from torch.optim.Adam based on:
    `Fixed Weight Decay Regularization in Adam`
    (see https://arxiv.org/abs/1711.05101)

    It has been proposed in `Adam: A Method for Stochastic Optimization`_.

    Args:
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float, optional): learning rate (default: 1e-3)
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        eps (float, optional): term added to the denominator to improve
            numerical stability (default: 1e-8)
        weight_decay (float, optional): weight decay (L2 penalty) (default: 0)
        amsgrad (boolean, optional): whether to use the AMSGrad variant of this
            algorithm from the paper `On the Convergence of Adam and Beyond`_

    .. _Adam\: A Method for Stochastic Optimization:
        https://arxiv.org/abs/1412.6980
    .. _On the Convergence of Adam and Beyond:
        https://openreview.net/forum?id=ryQu7f-RZ
    """

    def __init__(
        self,
        params,
        lr=1e-3,
        betas=(0.9, 0.999),
        eps=1e-8,
        weight_decay=0,
        amsgrad=False,
    ):
        defaults = dict(
            lr=lr, betas=betas, eps=eps, weight_decay=weight_decay, amsgrad=amsgrad
        )
        super(Adam, self).__init__(params, defaults)

    @property
    def supports_memory_efficient_fp16(self):
        return True

    @property
    def supports_flat_params(self):
        return True

    def step(self, closure=None):
        """Performs a single optimization step.

        Args:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                grad = p.grad.data
                if grad.dtype in {torch.float16, torch.bfloat16}:
                    grad = grad.float()
                if grad.is_sparse:
                    raise RuntimeError(
                        "Adam does not support sparse gradients, please consider SparseAdam instead"
                    )
                amsgrad = group.get("amsgrad", False)

                p_data_fp32 = p.data
                if p.data.dtype in {torch.float16, torch.bfloat16}:
                    p_data_fp32 = p_data_fp32.float()

                state = self.state[p]

                # State initialization
                if len(state) == 0:
                    state["step"] = 0
                    # Exponential moving average of gradient values
                    state["exp_avg"] = torch.zeros_like(p_data_fp32)
                    # Exponential moving average of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(p_data_fp32)
                    if amsgrad:
                        # Maintains max of all exp. moving avg. of sq. grad. values
                        state["max_exp_avg_sq"] = torch.zeros_like(p_data_fp32)
                else:
                    state["exp_avg"] = state["exp_avg"].to(p_data_fp32)
                    state["exp_avg_sq"] = state["exp_avg_sq"].to(p_data_fp32)
                    if amsgrad:
                        state["max_exp_avg_sq"] = state["max_exp_avg_sq"].to(
                            p_data_fp32
                        )

                exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]
                if amsgrad:
                    max_exp_avg_sq = state["max_exp_avg_sq"]
                beta1, beta2 = group["betas"]

                state["step"] += 1

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)
                exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)
                if amsgrad:
                    # Maintains the maximum of all 2nd moment running avg. till now
                    torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    # Use the max. for normalizing running avg. of gradient
                    denom = max_exp_avg_sq.sqrt().add_(group["eps"])
                else:
                    denom = exp_avg_sq.sqrt().add_(group["eps"])

                bias_correction1 = 1 - beta1 ** state["step"]
                bias_correction2 = 1 - beta2 ** state["step"]
                step_size = group["lr"] * math.sqrt(bias_correction2) / bias_correction1

                if group["weight_decay"] != 0:
                    p_data_fp32.add_(
                        p_data_fp32, alpha=-group["weight_decay"] * group["lr"]
                    )

                p_data_fp32.addcdiv_(exp_avg, denom, value=-step_size)

                if p.data.dtype in {torch.float16, torch.bfloat16}:
                    p.data.copy_(p_data_fp32)

        return loss

    @torch.no_grad()
    def remove_grads(self, _model):
        # remove gradient and exp avg, exp_avg_sq
        pm = _model.pm
        pd = pm.pruning_dict

        en_heads = _model.cfg.encoder.attention_heads
        de_heads = _model.cfg.decoder.attention_heads

        named_params = list(_model.named_parameters())
        param_list = []
        param_names = []
        for _n, _p in _model.named_parameters():
            if _n[-2:] == "_c":
                continue
            if not _p.requires_grad:
                continue
            param_list.append(_p)
            param_names.append(_n)
            
        def get_pruning_mask(max_len, pruning_indices):
            _mask = torch.ones(max_len).bool()
            _mask[pruning_indices] = False
            return _mask

        _i = -1
        for _k, _v in self.state.items():
            _i += 1
            _n = param_names[_i]
            _p = param_list[_i]
            """
            print('===================')
            print(_n) 
            print(_p.shape)
            print(_v['exp_avg'].shape)
            print('===================')
            """

            if not _p.requires_grad:
                continue

            _shape = _v['exp_avg'].shape
            if _n[-2:] == "_c" :
                continue
            elif 'embed_tokens' in _n:
                ende = _n.split('.')[0]
                _key = f"{ende}.embedding_c"
                _indices = pd[_key] if _key in pd else []

                _p.grad[:,_indices] = 0.
                _v['exp_avg'][:,_indices] = 0.
                _v['exp_avg_sq'][:,_indices] = 0.

            elif 'output_projection' in _n:
                continue

            elif 'layer_norm' in _n:
                ende, ly, type, wb = _parsing(_n)
                _key = f"{ende}.embedding_c"
                _indices = pd[_key] if _key in pd else []
                _p.grad[_indices] = 0.
                _v['exp_avg'][_indices] = 0.
                _v['exp_avg_sq'][_indices] = 0.

            elif 'fc' in _n:
                # fc layers
                # fc1: (gl_dim, fc_dim) | bias: fc_dim | global: prev_sub
                # fc2: (fc_dim, gl_dim) | bias: fc_dim | global: prev_sub
                ende, ly, type, wb = _parsing(_n)

                # Get global and local masks
                global_key = f'{ende}.embedding_c'
                local_key = f'{ende}.layers.{ly}.fc_c'

                global_indices = pd[global_key] if global_key in pd else []
                local_indices = pd[local_key] if local_key in pd else []
                
                if 'fc2' in _n:
                    if 'bias' in _n:
                        _p.grad[global_indices] = 0.
                        _v['exp_avg'][global_indices] = 0.
                        _v['exp_avg_sq'][global_indices] = 0.
                    else:
                        _p.grad[global_indices, :] = 0.
                        _p.grad[:, local_indices] = 0.

                        _v['exp_avg'][global_indices,:] = 0.
                        _v['exp_avg'][:,local_indices] = 0.
                        _v['exp_avg_sq'][global_indices,:] = 0.
                        _v['exp_avg_sq'][:,local_indices] = 0.

                else:
                    if 'bias' in _n:
                        _p.grad[local_indices] = 0.
                        _v['exp_avg'][local_indices] = 0.
                        _v['exp_avg_sq'][local_indices] = 0.
                    else:
                        _p.grad[:,global_indices] = 0.
                        _p.grad[local_indices,:] = 0.

                        _v['exp_avg'][:,global_indices] = 0.
                        _v['exp_avg'][local_indices,:] = 0.
                        _v['exp_avg_sq'][:,global_indices] = 0.
                        _v['exp_avg_sq'][local_indices,:] = 0.

            else:
                # qkvo_proj
                # q: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # k: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # v: (vo_dim, gl_dim) | bias: vo_dim | global: 
                # o: (gl_dim, vo_dim) | bias: gl_dim | global: previous sub-layer ln_c
                
                ende, ly, type, wb = _parsing(_n)
                # Get global and local masks
                if 'self_attn' in _n:
                    global_key = f'{ende}.embedding_c'
                    if 'q_proj' in _n or 'k_proj' in _n:
                        local_key = f'{ende}.layers.{ly}.self_attn_qk_c'
                    else:
                        local_key = f'{ende}.layers.{ly}.self_attn_vo_c'
                else:
                    # encoder_attn
                    if 'k_proj' in _n or 'v_proj' in _n:
                        global_key = 'encoder.embedding_c'
                    else:
                        global_key = 'decoder.embedding_c'

                    if 'q_proj' in _n or 'k_proj' in _n:
                        local_key = f'{ende}.layers.{ly}.encoder_attn_qk_c'
                    else:
                        local_key = f'{ende}.layers.{ly}.encoder_attn_vo_c'

                # q: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # k: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # v: (vo_dim, gl_dim) | bias: vo_dim | global: 

                global_indices = pd[global_key] if global_key in pd else []
                local_indices = pd[local_key] if local_key in pd else []

                if 'out_proj' in _n:
                    if 'bias' in _n:
                        _p.grad[global_indices] = 0.
                        _v['exp_avg'][global_indices] = 0.
                        _v['exp_avg_sq'][global_indices] = 0.
                    else:
                        _p.grad[global_indices, :] = 0.
                        _p.grad[:, local_indices] = 0.

                        _v['exp_avg'][global_indices,:] = 0.
                        _v['exp_avg'][:,local_indices] = 0.
                        _v['exp_avg_sq'][global_indices,:] = 0.
                        _v['exp_avg_sq'][:,local_indices] = 0.
                else:
                    if 'bias' in _n:
                        _p.grad[local_indices] = 0.
                        _v['exp_avg'][local_indices] = 0.
                        _v['exp_avg_sq'][local_indices] = 0.
                    else:
                        _p.grad[:,global_indices] = 0.
                        _p.grad[local_indices,:] = 0.

                        _v['exp_avg'][:,global_indices] = 0.
                        _v['exp_avg'][local_indices,:] = 0.
                        _v['exp_avg_sq'][:,global_indices] = 0.
                        _v['exp_avg_sq'][local_indices,:] = 0.

        # self.state = _dict
        


    def pruning(self, _model):
        pm = _model.pm
        pd = pm.pruning_dict

        en_heads = _model.cfg.encoder.attention_heads
        de_heads = _model.cfg.decoder.attention_heads

        named_params = list(_model.named_parameters())
        param_list = []
        param_names = []

        for _n, _p in _model.named_parameters():
            if '_indices' in _n or 'mask' in _n:
                continue
            if _n[-2:] == "_c" or not _p.requires_grad:
                continue
            param_list.append(_p)
            param_names.append(_n)
            
        self.param_groups[0]['params'] = param_list
        _dict = {}

        def get_pruning_mask(max_len, pruning_indices):
            _mask = torch.ones(max_len).bool()
            _mask[pruning_indices] = False
            return _mask

        _i = 0
        for _k, _v in self.state.items():
            _n = param_names[_i]
            _shape = _v['exp_avg'].shape
            # print("*** ", _n, ": ", _shape)
            if _n[-2:] == "_c" :
                continue
            elif 'embed_tokens' in _n:
                ende = _n.split('.')[0]
                _key = f"{ende}.embedding_c"
                mask = get_pruning_mask(_shape[1], pd[_key])
                _v['exp_avg'] = _v['exp_avg'][:, mask]
                _v['exp_avg_sq'] = _v['exp_avg_sq'][:, mask]
            elif 'output_projection' in _n:
                continue

            elif 'layer_norm' in _n:
                ende, ly, type, wb = _parsing(_n)
                if 'self' in type:
                    _type = 'self_attn'
                elif 'encoder' in type:
                    _type = 'encoder_attn'
                else:
                    _type = 'fc'
                _key = f"{ende}.embedding_c"
                mask = get_pruning_mask(_shape[0], pd[_key])

                _v['exp_avg'] = _v['exp_avg'][mask]
                _v['exp_avg_sq'] = _v['exp_avg_sq'][mask]

            elif 'fc' in _n:
                # fc layers
                # fc1: (gl_dim, fc_dim) | bias: fc_dim | global: prev_sub
                # fc2: (fc_dim, gl_dim) | bias: fc_dim | global: prev_sub
                ende, ly, type, wb = _parsing(_n)

                # Get global and local masks
                global_key = f'{ende}.embedding_c'
                local_key = f'{ende}.layers.{ly}.fc_c'

                global_indices = pd[global_key] if global_key in pd else []
                local_indices = pd[local_key] if local_key in pd else []

                if 'fc2' in _n:
                    if 'bias' in _n:
                        global_mask = get_pruning_mask(_shape[0],  global_indices)
                        _v['exp_avg'] = _v['exp_avg'][global_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][global_mask]
                    else:
                        global_mask = get_pruning_mask(_shape[0],  global_indices)
                        local_mask = get_pruning_mask(_shape[1],  local_indices)
                        _v['exp_avg'] = _v['exp_avg'][global_mask,:][:,local_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][global_mask,:][:,local_mask]
                else:
                    if 'bias' in _n:
                        local_mask = get_pruning_mask(_shape[0],  local_indices)
                        _v['exp_avg'] = _v['exp_avg'][local_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][local_mask]
                    else:
                        global_mask = get_pruning_mask(_shape[1],  global_indices)
                        local_mask = get_pruning_mask(_shape[0],  local_indices)
                        _v['exp_avg'] = _v['exp_avg'][local_mask,:][:,global_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][local_mask,:][:,global_mask]
            else:
                # qkvo_proj
                # q: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # k: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # v: (vo_dim, gl_dim) | bias: vo_dim | global: 
                # o: (gl_dim, vo_dim) | bias: gl_dim | global: previous sub-layer ln_c
                
                ende, ly, type, wb = _parsing(_n)
                # Get global and local masks
                if 'self_attn' in _n:
                    global_key = f'{ende}.embedding_c'
                    if 'q_proj' in _n or 'k_proj' in _n:
                        local_key = f'{ende}.layers.{ly}.self_attn_qk_c'
                    else:
                        local_key = f'{ende}.layers.{ly}.self_attn_vo_c'
                else:
                    # encoder_attn
                    if 'k_proj' in _n or 'v_proj' in _n:
                        global_key = f'encoder.embedding_c'
                    else:
                        global_key = f'decoder.embedding_c'

                    if 'q_proj' in _n or 'k_proj' in _n:
                        local_key = f'{ende}.layers.{ly}.encoder_attn_qk_c'
                    else:
                        local_key = f'{ende}.layers.{ly}.encoder_attn_vo_c'

                # q: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # k: (qk_dim, gl_dim) | bias: qk_dim | global: 
                # v: (vo_dim, gl_dim) | bias: vo_dim | global: 

                global_indices = pd[global_key] if global_key in pd else []
                local_indices = pd[local_key] if local_key in pd else []

                # Compute loss 
                if 'out_proj' in _n:
                    if 'bias' in _n:
                        global_mask = get_pruning_mask(_shape[0],  global_indices)
                        _v['exp_avg'] = _v['exp_avg'][global_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][global_mask]
                    else:
                        global_mask = get_pruning_mask(_shape[0],  global_indices)
                        local_mask = get_pruning_mask(_shape[1],  local_indices)
                        _v['exp_avg'] = _v['exp_avg'][global_mask,:][:,local_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][global_mask,:][:,local_mask]
                else:
                    if 'bias' in _n:
                        local_mask = get_pruning_mask(_shape[0],  local_indices)
                        _v['exp_avg'] = _v['exp_avg'][local_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][local_mask]
                    else:
                        global_mask = get_pruning_mask(_shape[1],  global_indices)
                        local_mask = get_pruning_mask(_shape[0],  local_indices)
                        _v['exp_avg'] = _v['exp_avg'][local_mask,:][:,global_mask]
                        _v['exp_avg_sq'] = _v['exp_avg_sq'][local_mask,:][:,global_mask]
            _dict[param_list[_i]] = _v
            _i+=1
        self.state = _dict


def _parsing(_name):
    assert 'embed_tokens' not in _name
    _l = _name.split('.')
    if 'attn' in _name and 'layer_norm' not in _name:
        ende, ly, type, wb = _l[0], _l[2], f'{_l[3]}.{_l[4]}',_l[5]
    else:
        try:
            ende, ly, type, wb = _l[0], _l[2], _l[3],_l[4]
        except Exception:
            print("* Name: ", _name)
    return ende, ly, type, wb

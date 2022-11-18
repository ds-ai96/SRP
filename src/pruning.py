#!/usr/bin/env python3 -u
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""
Train a new model on one or across multiple GPUs.
"""

import argparse
import logging
import math
import os
import sys
import pickle, time, copy
from typing import Any, Callable, Dict, List, Optional, Tuple

# We need to setup root logger before importing any fairseq libraries.
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
    stream=sys.stdout,
)
logger = logging.getLogger("fairseq_cli.train")

import numpy as np
import torch
from omegaconf import DictConfig, OmegaConf

import checkpoint_utils
from flops_counter import FLOPS_COUNTER
from fairseq import options, quantization_utils, tasks, utils
from fairseq.data import data_utils, iterators
from fairseq.data.plasma_utils import PlasmaStore
from fairseq.dataclass.configs import FairseqConfig
from fairseq.dataclass.initialize import add_defaults
from fairseq.dataclass.utils import convert_namespace_to_omegaconf
from fairseq.distributed import fsdp_enable_wrap, fsdp_wrap
from fairseq.distributed import utils as distributed_utils
from fairseq.file_io import PathManager
from fairseq.logging import meters, metrics, progress_bar
from fairseq.model_parallel.megatron_trainer import MegatronTrainer
from trainer import Trainer



def main(cfg: FairseqConfig) -> None:
    if isinstance(cfg, argparse.Namespace):
        cfg = convert_namespace_to_omegaconf(cfg)

    utils.import_user_module(cfg.common)
    add_defaults(cfg)

    if (
        distributed_utils.is_master(cfg.distributed_training)
        and "job_logging_cfg" in cfg
    ):
        # make hydra logging work with ddp (see # see https://github.com/facebookresearch/hydra/issues/1126)
        logging.config.dictConfig(OmegaConf.to_container(cfg.job_logging_cfg))

    assert (
        cfg.dataset.max_tokens is not None or cfg.dataset.batch_size is not None
    ), "Must specify batch size either with --max-tokens or --batch-size"
    metrics.reset()

    if cfg.common.log_file is not None:
        handler = logging.FileHandler(filename=cfg.common.log_file)
        logger.addHandler(handler)

    np.random.seed(cfg.common.seed)
    utils.set_torch_seed(cfg.common.seed)

    if distributed_utils.is_master(cfg.distributed_training):
        checkpoint_utils.verify_checkpoint_directory(cfg.checkpoint.save_dir)

    # Print args
    logger.info(cfg)

    if cfg.checkpoint.write_checkpoints_asynchronously:
        try:
            import iopath  # noqa: F401
        except ImportError:
            logging.exception(
                "Asynchronous checkpoint writing is specified but iopath is "
                "not installed: `pip install iopath`"
            )
            return

    # Setup task, e.g., translation, language modeling, etc.
    task = tasks.setup_task(cfg.task)

    assert cfg.criterion, "Please specify criterion to train a model"

    # Build model and criterion
    if cfg.distributed_training.ddp_backend == "fully_sharded":
        with fsdp_enable_wrap(cfg.distributed_training):
            model = fsdp_wrap(task.build_model(cfg.model))
    else:
        model = task.build_model(cfg.model)

    ############# Perform shaping Model for loading Pruned Model #################
    # pass checkpoint path and shaving model
    # pretrained_model = f'{cfg.checkpoint.save_dir}/{cfg.checkpoint.restore_file}'
    pretrained_model = cfg.model.pretrained_model
    if os.path.isfile(pretrained_model):
        # print("+++++++ Loading pre-trained model for finetuning +++++++")
        model = checkpoint_utils.load_spt(pretrained_model, model)
        # print("+++++ Loading pre-trained model for finetuning done +++++")
    ##############################################################################

    criterion = task.build_criterion(cfg.criterion)
    logger.info(model)
    logger.info("task: {}".format(task.__class__.__name__))
    logger.info("model: {}".format(model.__class__.__name__))
    logger.info("criterion: {}".format(criterion.__class__.__name__))
    logger.info(
        "num. shared model params: {:,} (num. trained: {:,})".format(
            sum(
                p.numel() for p in model.parameters() if not getattr(p, "expert", False)
            ),
            sum(
                p.numel()
                for p in model.parameters()
                if not getattr(p, "expert", False) and p.requires_grad
            ),
        )
    )

    logger.info(
        "num. expert model params: {} (num. trained: {})".format(
            sum(p.numel() for p in model.parameters() if getattr(p, "expert", False)),
            sum(
                p.numel()
                for p in model.parameters()
                if getattr(p, "expert", False) and p.requires_grad
            ),
        )
    )

    # Load valid dataset (we load training data below, based on the latest checkpoint)
    # We load the valid dataset AFTER building the model
    data_utils.raise_if_valid_subsets_unintentionally_ignored(cfg)
    if cfg.dataset.combine_valid_subsets:
        task.load_dataset("valid", combine=True, epoch=1)
    else:
        for valid_sub_split in cfg.dataset.valid_subset.split(","):
            task.load_dataset(valid_sub_split, combine=False, epoch=1)

    # (optionally) Configure quantization
    if cfg.common.quantization_config_path is not None:
        quantizer = quantization_utils.Quantizer(
            config_path=cfg.common.quantization_config_path,
            max_epoch=cfg.optimization.max_epoch,
            max_update=cfg.optimization.max_update,
        )
    else:
        quantizer = None

    # Build trainer
    if cfg.common.model_parallel_size == 1:
        trainer = Trainer(cfg, task, model, criterion, quantizer)
    else:
        trainer = MegatronTrainer(cfg, task, model, criterion)
    logger.info(
        "training on {} devices (GPUs/TPUs)".format(
            cfg.distributed_training.distributed_world_size
        )
    )
    logger.info(
        "max tokens per device = {} and max sentences per device = {}".format(
            cfg.dataset.max_tokens,
            cfg.dataset.batch_size,
        )
    )

    # Load the latest checkpoint if one is available and restore the
    # corresponding train iterator
    ##################### For SPT ################################
    extra_state, epoch_itr = checkpoint_utils.load_checkpoint(
        cfg.checkpoint,
        trainer,
        # don't cache epoch iterators for sharded datasets
        disable_iterator_cache=task.has_sharded_data("train"),
    )

    if extra_state is not None and 'pruning_manager' in extra_state:
        trainer.model.pm = extra_state['pruning_manager']
    ##############################################################

    max_epoch = cfg.optimization.max_epoch or math.inf
    lr = trainer.get_lr()

    train_meter = meters.StopwatchMeter()
    train_meter.start()

    ######################## For STP #######################
    # Load sample dataset for pruning
    # Get samples
    with open(f'../data-bin/iwslt14.tokenized.de-en/samples/samples0.pkl', 'rb') as f:
        samples = pickle.load(f)
    # print("##############3 ", getattr(cfg.model, 'srp', False))
    
    if getattr(cfg.model, 'srp', False):
        # Remove --srp argument for instant pruning
        # Pruning with SRP
        trainer.teacher_model = copy.deepcopy(trainer.model)
        trainer.model.phase = 'pruning'
        trainer.train_step(samples, scoring=True)
        _pm = trainer.model.pm
        gle, gld, fc, qk, vo = _pm.get(cfg.model.pruning_stage)

        if gle == -1:
            # Already setted
            pass
        else:
            # print("#"*85)
            # print(f"* Groups to remove: GLE ({gle}) | GLD ({gld}) | FC ({fc})  | QK ({qk}) | VO ({vo})")
            # scoring_groups(trainer.model)
            pruning_dict = {}
            pruning_dict.update(
                _pm.get_fc_dict(trainer.model, fc)
            )
            pruning_dict.update(
                _pm.get_global_dict(trainer.model, gle, "encoder")
            )
            pruning_dict.update(
                _pm.get_global_dict(trainer.model, gld, "decoder")
            )
            pruning_dict.update(
                _pm.get_qkvo_dict(trainer.model, qk, "qk")
            )
            pruning_dict.update(
                _pm.get_qkvo_dict(trainer.model, qk, "vo")
            )

            _pm.pruning_dict = pruning_dict           
            # print("#"*85)

        trainer.model.zero_grad()
        trainer.zero_grad()
    else:
        # Pruning without SRP
        logger.info(f"*** Scoring Start ***")
        trainer.model.phase = 'pruning'
        trainer.train_step(samples, scoring=True)
        # Scoring groups at the beginning of every epoch
        _pm = trainer.model.pm
        gle, gld, fc, qk, vo = _pm.get()

        # scoring_groups(trainer.model)
        pruning_dict = {}
        pruning_dict.update(
            _pm.get_fc_dict(trainer.model, fc)
        )
        pruning_dict.update(
            _pm.get_global_dict(trainer.model, gle, "encoder")
        )
        pruning_dict.update(
            _pm.get_global_dict(trainer.model, gld, "decoder")
        )
        pruning_dict.update(
            _pm.get_qkvo_dict(trainer.model, qk, "qk")
        )
        pruning_dict.update(
            _pm.get_qkvo_dict(trainer.model, qk, "vo")
        )

        # for k in pruning_dict:
        #     print(k, type(pruning_dict[k]))
        _pm.pruning_dict = pruning_dict           
        # print(_pm.pruning_dict)
        # print("#"*85)
        logger.info(f"*** Pruning Strart ***")
        trainer.model.pruning()
        # trainer.optimizer._optimizer.pruning(trainer.model)
        trainer.model.update_pos_emb_mask()
        
        num_params = np.sum([p.numel() for p in trainer.model.parameters() if p.requires_grad])
        
        param_dict = trainer.model.state_dict()
        s = 50
        heads = 4
        num_layers = 6
        emb = param_dict[f'encoder.embedding_c'].shape[0]
        en_self_qks = [param_dict[f'encoder.layers.{l}.self_attn_qk_c'].shape[0] 
                for l in range(num_layers)]
        en_self_vos = [param_dict[f'encoder.layers.{l}.self_attn_vo_c'].shape[0]
                for l in range(num_layers)]
        en_fcs = [param_dict[f'encoder.layers.{l}.fc_c'].shape[0] for l in range(num_layers)]

        de_self_qks = [param_dict[f'decoder.layers.{l}.self_attn_qk_c'].shape[0] \
                for l in range(num_layers)]
        de_self_vos = [param_dict[f'decoder.layers.{l}.self_attn_vo_c'].shape[0] \
                for l in range(num_layers)]
        de_encoder_qks = [param_dict[f'decoder.layers.{l}.encoder_attn_qk_c'].shape[0] \
                for l in range(num_layers)]
        de_encoder_vos = [param_dict[f'decoder.layers.{l}.encoder_attn_vo_c'].shape[0] \
                for l in range(num_layers)]
        de_fcs = [param_dict[f'decoder.layers.{l}.fc_c'].shape[0] for l in range(num_layers)]

        tar_dict_size = 6632

        fl_counter = FLOPS_COUNTER(s, emb, heads,
                    en_self_qks, en_self_vos, en_fcs,
                    de_self_qks, de_self_vos, de_fcs,
                    de_encoder_qks, de_encoder_vos,
                    tar_dict_size)
        print("\n=======================================================")
        num_groups = trainer.model.get_num_groups()
        print(num_groups)
        print(f"**  Num params after pruning: {num_params/1000000:.3f}M")
        print(f"**  Num FLOPS after pruing: {fl_counter.get_model_flops()/1e9:.3f}")
        print("=======================================================\n")

        trainer.train_step(samples, scoring=True)
        trainer.model.zero_grad()
        trainer.zero_grad()
        # torch.save(trainer.model.state_dict(), 
        #         f'{cfg.checkpoint.save_dir}/pruned.pt')
        itr = epoch_itr.next_epoch_itr(
            fix_batches_to_gpus=cfg.distributed_training.fix_batches_to_gpus,
            shuffle=(epoch_itr.next_epoch_idx > cfg.dataset.curriculum),
        )

        checkpoint_utils.save_checkpoint(
            cfg.checkpoint, trainer, epoch_itr, None
        )
        print("Save pruned model")
        return
    


    # phase: 'warming-up', 'pruning' or 'fine-tuning'
    # setattr(trainer.model, 'phase', 'warming-up')
    pruning_count = 0
    #########################################################

    is_first_epoch = True
    while epoch_itr.next_epoch_idx <= max_epoch:
        # Determine phase and performe pruning
        print(trainer.model.encoder.embedding_c[0:20])
        
        if is_first_epoch:
            _epoch = epoch_itr.epoch
            is_first_epoch = False
        else:   
            _epoch = epoch_itr.epoch + 1

        _phase, do_pruning = trainer.model.pm.get_phase(_epoch)
        logger.info(f"Epoch {_epoch} | phase: {_phase}")
        setattr(trainer.model, 'phase', _phase)
        
        if do_pruning:
            pruning_count += 1

        if lr <= cfg.optimization.stop_min_lr:
            logger.info(
                f"stopping training because current learning rate ({lr}) is smaller "
                "than or equal to minimum learning rate "
                f"(--stop-min-lr={cfg.optimization.stop_min_lr})"
            )
            break
        # print("* Current embedding_c: ",  trainer.model.decoder.embedding_c)
        # train for one epoch
        valid_losses, should_stop = train(cfg, trainer, task, epoch_itr,
                                          do_pruning=do_pruning)
        # print("* After training an epoch: ", epoch_itr.epoch)
        # Check pruning target
        _params = np.sum([_p.numel() for _n, _p in trainer.model.named_parameters()
                          if _n[-2:] != '_c'])
        num_groups = trainer.model.get_num_groups()
        num_groups = [str(_num) for _num in num_groups]
        
        ##################### SPT  Pruning ##########################
        # print pruning status        
        _res = f'{_phase[0]},{epoch_itr.epoch},'
        _res+= ','.join(num_groups) + ','
        # _group_res = group_report(trainer.model, gl_dict)
        # _res += _group_res
        _res += f'{_params},{valid_losses[0]}'
        # print("+"*15, '  Test ', '+'*15)
        logger.info(_res)
        _path_list = cfg.checkpoint.save_dir.split('/')
        _res_file = f'../checkpoints/res_files/{_path_list[-1]}.csv'
        logger.info(f"Result file: {_res_file}")
        # print("+"*15, '  Test ', '+'*15)
        with open(_res_file, 'a') as f:
            f.write(_res + '\n')
        
        # Save pruning status (param/ bleu/ groups change)
        ##############################################################

        if should_stop:
            break

        # only use first validation loss to update the learning rate
        lr = trainer.lr_step(epoch_itr.epoch, valid_losses[0])

        epoch_itr = trainer.get_train_iterator(
            epoch_itr.next_epoch_idx,
            # sharded data: get train iterator for next epoch
            load_dataset=task.has_sharded_data("train"),
            # don't cache epoch iterators for sharded datasets
            disable_iterator_cache=task.has_sharded_data("train"),
        )
    train_meter.stop()
    logger.info("done training in {:.1f} seconds".format(train_meter.sum))

    # ioPath implementation to wait for all asynchronous file writes to complete.
    if cfg.checkpoint.write_checkpoints_asynchronously:
        logger.info(
            "ioPath PathManager waiting for all asynchronous checkpoint "
            "writes to finish."
        )
        PathManager.async_close()
        logger.info("ioPath PathManager finished waiting.")



def should_stop_early(cfg: DictConfig, valid_loss: float) -> bool:
    # skip check if no validation was done in the current epoch
    if valid_loss is None:
        return False
    if cfg.checkpoint.patience <= 0:
        return False

    def is_better(a, b):
        return a > b if cfg.checkpoint.maximize_best_checkpoint_metric else a < b

    prev_best = getattr(should_stop_early, "best", None)
    if prev_best is None or is_better(valid_loss, prev_best):
        should_stop_early.best = valid_loss
        should_stop_early.num_runs = 0
        return False
    else:
        should_stop_early.num_runs += 1
        if should_stop_early.num_runs >= cfg.checkpoint.patience:
            logger.info(
                "early stop since valid performance hasn't improved for last {} runs".format(
                    cfg.checkpoint.patience
                )
            )
            return True
        else:
            return False


@metrics.aggregate("train")
def train(
    cfg: DictConfig, trainer: Trainer, task: tasks.FairseqTask, epoch_itr,
    do_pruning=False
) -> Tuple[List[Optional[float]], bool]:
    """Train the model for one epoch and return validation losses."""
    # Initialize data iterator
    itr = epoch_itr.next_epoch_itr(
        fix_batches_to_gpus=cfg.distributed_training.fix_batches_to_gpus,
        shuffle=(epoch_itr.next_epoch_idx > cfg.dataset.curriculum),
    )
    update_freq = (
        cfg.optimization.update_freq[epoch_itr.epoch - 1]
        if epoch_itr.epoch <= len(cfg.optimization.update_freq)
        else cfg.optimization.update_freq[-1]
    )
    itr = iterators.GroupedIterator(
        itr,
        update_freq,
        skip_remainder_batch=cfg.optimization.skip_remainder_batch,
    )
    if cfg.common.tpu:
        itr = utils.tpu_data_loader(itr)
    progress = progress_bar.progress_bar(
        itr,
        log_format=cfg.common.log_format,
        log_file=cfg.common.log_file,
        log_interval=cfg.common.log_interval,
        epoch=epoch_itr.epoch,
        aim_repo=(
            cfg.common.aim_repo
            if distributed_utils.is_master(cfg.distributed_training)
            else None
        ),
        aim_run_hash=(
            cfg.common.aim_run_hash
            if distributed_utils.is_master(cfg.distributed_training)
            else None
        ),
        aim_param_checkpoint_dir=cfg.checkpoint.save_dir,
        tensorboard_logdir=(
            cfg.common.tensorboard_logdir
            if distributed_utils.is_master(cfg.distributed_training)
            else None
        ),
        default_log_format=("tqdm" if not cfg.common.no_progress_bar else "simple"),
        wandb_project=(
            cfg.common.wandb_project
            if distributed_utils.is_master(cfg.distributed_training)
            else None
        ),
        wandb_run_name=os.environ.get(
            "WANDB_NAME", os.path.basename(cfg.checkpoint.save_dir)
        ),
        azureml_logging=(
            cfg.common.azureml_logging
            if distributed_utils.is_master(cfg.distributed_training)
            else False
        ),
    )
    progress.update_config(_flatten_config(cfg))

    trainer.begin_epoch(epoch_itr.epoch)

    valid_subsets = cfg.dataset.valid_subset.split(",")
    should_stop = False
    num_updates = trainer.get_num_updates()
    logger.info("Start iterating over samples")

    ################### SPT #####################
    _decreasing = cfg.model.decreasing
    _pm = trainer.model.pm

    # Comment this for testing
    
    if trainer.model.phase == 'pruning':
        # Epoch-wise decreasing
        if _decreasing[0] == 'e':
            trainer.model.decrease_c()
    
    ################################################
    for i, samples in enumerate(progress):
        if _decreasing[0] == 's':
            if trainer.model.phase == 'pruning':
                trainer.model.decrease_c()
        with metrics.aggregate("train_inner"), torch.autograd.profiler.record_function(
            "train_step-%d" % i
        ):
            log_output = trainer.train_step(samples, scoring=False)

        if log_output is not None:  # not OOM, overflow, ...
            # log mid-epoch stats
            num_updates = trainer.get_num_updates()
            if num_updates % cfg.common.log_interval == 0:
                stats = get_training_stats(metrics.get_smoothed_values("train_inner"))
                progress.log(stats, tag="train_inner", step=num_updates)

                # reset mid-epoch stats after each log interval
                # the end-of-epoch stats will still be preserved
                metrics.reset_meters("train_inner")

        end_of_epoch = not itr.has_next()

        ####################### Perform Pruning #############################
        if end_of_epoch and do_pruning:
            logger.info(f"*** Perform pruning ***")
            trainer.model.pruning()
            trainer.optimizer._optimizer.pruning(trainer.model)
            if trainer.model.cfg.pruning_stage != 2:
                trainer.model.update_pos_emb_mask()
        ####################################################################

        valid_losses, should_stop = validate_and_save(
            cfg, trainer, task, epoch_itr, valid_subsets, end_of_epoch
        )

        if should_stop:
            break

    # log end-of-epoch stats
    logger.info("end of epoch {} (average epoch stats below)".format(epoch_itr.epoch))
    stats = get_training_stats(metrics.get_smoothed_values("train"))
    progress.print(stats, tag="train", step=num_updates)

    # reset epoch-level meters
    metrics.reset_meters("train")
    return valid_losses, should_stop


def _flatten_config(cfg: DictConfig):
    config = OmegaConf.to_container(cfg)
    # remove any legacy Namespaces and replace with a single "args"
    namespace = None
    for k, v in list(config.items()):
        if isinstance(v, argparse.Namespace):
            namespace = v
            del config[k]
    if namespace is not None:
        config["args"] = vars(namespace)
    return config


def validate_and_save(
    cfg: DictConfig,
    trainer: Trainer,
    task: tasks.FairseqTask,
    epoch_itr,
    valid_subsets: List[str],
    end_of_epoch: bool,
) -> Tuple[List[Optional[float]], bool]:
    num_updates = trainer.get_num_updates()
    max_update = cfg.optimization.max_update or math.inf

    # Stopping conditions (and an additional one based on validation loss later
    # on)
    should_stop = False
    if num_updates >= max_update:
        should_stop = True
        logger.info(
            f"Stopping training due to "
            f"num_updates: {num_updates} >= max_update: {max_update}"
        )

    training_time_hours = trainer.cumulative_training_time() / (60 * 60)
    if (
        cfg.optimization.stop_time_hours > 0
        and training_time_hours > cfg.optimization.stop_time_hours
    ):
        should_stop = True
        logger.info(
            f"Stopping training due to "
            f"cumulative_training_time: {training_time_hours} > "
            f"stop_time_hours: {cfg.optimization.stop_time_hours} hour(s)"
        )

    do_save = (
        (end_of_epoch and epoch_itr.epoch % cfg.checkpoint.save_interval == 0)
        or should_stop
        or (
            cfg.checkpoint.save_interval_updates > 0
            and num_updates > 0
            and num_updates % cfg.checkpoint.save_interval_updates == 0
            and num_updates >= cfg.dataset.validate_after_updates
        )
    )
    do_validate = (
        (
            (not end_of_epoch and do_save)  # validate during mid-epoch saves
            or (end_of_epoch and epoch_itr.epoch % cfg.dataset.validate_interval == 0)
            or should_stop
            or (
                cfg.dataset.validate_interval_updates > 0
                and num_updates > 0
                and num_updates % cfg.dataset.validate_interval_updates == 0
            )
        )
        and not cfg.dataset.disable_validation
        and num_updates >= cfg.dataset.validate_after_updates
    )

    # Validate
    valid_losses = [None]
    if do_validate:
        valid_losses = validate(cfg, trainer, task, epoch_itr, valid_subsets)

    should_stop |= should_stop_early(cfg, valid_losses[0])

    # Save checkpoint
    if do_save or should_stop:
        checkpoint_utils.save_checkpoint(
            cfg.checkpoint, trainer, epoch_itr, valid_losses[0]
        )

    return valid_losses, should_stop


def get_training_stats(stats: Dict[str, Any]) -> Dict[str, Any]:
    stats["wall"] = round(metrics.get_meter("default", "wall").elapsed_time, 0)
    return stats


def validate(
    cfg: DictConfig,
    trainer: Trainer,
    task: tasks.FairseqTask,
    epoch_itr,
    subsets: List[str],
) -> List[Optional[float]]:
    """Evaluate the model on the validation set(s) and return the losses."""

    if cfg.dataset.fixed_validation_seed is not None:
        # set fixed seed for every validation
        utils.set_torch_seed(cfg.dataset.fixed_validation_seed)

    trainer.begin_valid_epoch(epoch_itr.epoch)
    valid_losses = []
    for subset_idx, subset in enumerate(subsets):
        logger.info('begin validation on "{}" subset'.format(subset))

        # Initialize data iterator
        itr = trainer.get_valid_iterator(subset).next_epoch_itr(
            shuffle=False, set_dataset_epoch=False  # use a fixed valid set
        )
        if cfg.common.tpu:
            itr = utils.tpu_data_loader(itr)
        progress = progress_bar.progress_bar(
            itr,
            log_format=cfg.common.log_format,
            log_interval=cfg.common.log_interval,
            epoch=epoch_itr.epoch,
            prefix=f"valid on '{subset}' subset",
            aim_repo=(
                cfg.common.aim_repo
                if distributed_utils.is_master(cfg.distributed_training)
                else None
            ),
            aim_run_hash=(
                cfg.common.aim_run_hash
                if distributed_utils.is_master(cfg.distributed_training)
                else None
            ),
            aim_param_checkpoint_dir=cfg.checkpoint.save_dir,
            tensorboard_logdir=(
                cfg.common.tensorboard_logdir
                if distributed_utils.is_master(cfg.distributed_training)
                else None
            ),
            default_log_format=("tqdm" if not cfg.common.no_progress_bar else "simple"),
            wandb_project=(
                cfg.common.wandb_project
                if distributed_utils.is_master(cfg.distributed_training)
                else None
            ),
            wandb_run_name=os.environ.get(
                "WANDB_NAME", os.path.basename(cfg.checkpoint.save_dir)
            ),
        )

        # create a new root metrics aggregator so validation metrics
        # don't pollute other aggregators (e.g., train meters)
        with metrics.aggregate(new_root=True) as agg:
            for i, sample in enumerate(progress):
                if (
                    cfg.dataset.max_valid_steps is not None
                    and i > cfg.dataset.max_valid_steps
                ):
                    break
                trainer.valid_step(sample)

        # log validation stats
        # only tracking the best metric on the 1st validation subset
        tracking_best = subset_idx == 0
        stats = get_valid_stats(cfg, trainer, agg.get_smoothed_values(), tracking_best)

        if hasattr(task, "post_validate"):
            task.post_validate(trainer.get_model(), stats, agg)

        progress.print(stats, tag=subset, step=trainer.get_num_updates())

        valid_losses.append(stats[cfg.checkpoint.best_checkpoint_metric])
    return valid_losses


def get_valid_stats(
    cfg: DictConfig,
    trainer: Trainer,
    stats: Dict[str, Any],
    tracking_best: bool,
) -> Dict[str, Any]:
    stats["num_updates"] = trainer.get_num_updates()
    if tracking_best and hasattr(checkpoint_utils.save_checkpoint, "best"):
        key = "best_{0}".format(cfg.checkpoint.best_checkpoint_metric)
        best_function = max if cfg.checkpoint.maximize_best_checkpoint_metric else min
        stats[key] = best_function(
            checkpoint_utils.save_checkpoint.best,
            stats[cfg.checkpoint.best_checkpoint_metric],
        )
    return stats


def cli_main(
    modify_parser: Optional[Callable[[argparse.ArgumentParser], None]] = None
) -> None:
    parser = options.get_training_parser()
    args = options.parse_args_and_arch(parser, modify_parser=modify_parser)

    cfg = convert_namespace_to_omegaconf(args)

    if cfg.common.use_plasma_view:
        server = PlasmaStore(path=cfg.common.plasma_path)
        logger.info(
            f"Started plasma server pid {server.server.pid} {cfg.common.plasma_path}"
        )

    if args.profile:
        with torch.cuda.profiler.profile():
            with torch.autograd.profiler.emit_nvtx():
                distributed_utils.call_main(cfg, main)
    else:
        distributed_utils.call_main(cfg, main)

    # if cfg.common.use_plasma_view:
    #     server.server.kill()


if __name__ == "__main__":
    cli_main()

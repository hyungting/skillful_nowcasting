import torch.utils.data.dataset
from dgmr import DGMR
from datasets import load_dataset
from torch.utils.data import DataLoader
from pytorch_lightning import (
    LightningDataModule,
)
from pytorch_lightning.callbacks import ModelCheckpoint
import wandb
wandb.init(project="dgmr")
from numpy.random import default_rng
import os
import numpy as np
from pathlib import Path
import tensorflow as tf
from pytorch_lightning import Callback, Trainer
from pytorch_lightning.loggers import LoggerCollection, WandbLogger
from pytorch_lightning.utilities import rank_zero_only


def get_wandb_logger(trainer: Trainer) -> WandbLogger:
    """Safely get Weights&Biases logger from Trainer."""

    if trainer.fast_dev_run:
        raise Exception(
            "Cannot use wandb callbacks since pytorch lightning disables loggers in `fast_dev_run=true` mode."
        )

    if isinstance(trainer.logger, WandbLogger):
        return trainer.logger

    if isinstance(trainer.logger, LoggerCollection):
        for logger in trainer.logger:
            if isinstance(logger, WandbLogger):
                return logger

    raise Exception(
        "You are using wandb related callback, but WandbLogger was not found for some reason..."
    )


class WatchModel(Callback):
    """Make wandb watch model at the beginning of the run."""

    def __init__(self, log: str = "gradients", log_freq: int = 100):
        self.log = log
        self.log_freq = log_freq

    @rank_zero_only
    def on_train_start(self, trainer, pl_module):
        logger = get_wandb_logger(trainer=trainer)
        logger.watch(model=trainer.model, log=self.log, log_freq=self.log_freq, log_graph=True)


class UploadCheckpointsAsArtifact(Callback):
    """Upload checkpoints to wandb as an artifact, at the end of run."""

    def __init__(self, ckpt_dir: str = "checkpoints/", upload_best_only: bool = False):
        self.ckpt_dir = ckpt_dir
        self.upload_best_only = upload_best_only

    @rank_zero_only
    def on_keyboard_interrupt(self, trainer, pl_module):
        self.on_train_end(trainer, pl_module)

    @rank_zero_only
    def on_train_end(self, trainer, pl_module):
        logger = get_wandb_logger(trainer=trainer)
        experiment = logger.experiment

        ckpts = wandb.Artifact("experiment-ckpts", type="checkpoints")

        if self.upload_best_only:
            ckpts.add_file(trainer.checkpoint_callback.best_model_path)
        else:
            for path in Path(self.ckpt_dir).rglob("*.ckpt"):
                ckpts.add_file(str(path))

        experiment.log_artifact(ckpts)

    @rank_zero_only
    def on_validation_epoch_end(self, trainer, pl_module):
        logger = get_wandb_logger(trainer=trainer)
        experiment = logger.experiment

        ckpts = wandb.Artifact("experiment-ckpts", type="checkpoints")

        if self.upload_best_only:
            ckpts.add_file(trainer.checkpoint_callback.best_model_path)
        else:
            for path in Path(self.ckpt_dir).rglob("*.ckpt"):
                ckpts.add_file(str(path))

        experiment.log_artifact(ckpts)

    @rank_zero_only
    def on_train_epoch_end(self, trainer, pl_module):
        logger = get_wandb_logger(trainer=trainer)
        experiment = logger.experiment

        ckpts = wandb.Artifact("experiment-ckpts", type="checkpoints")

        if self.upload_best_only:
            ckpts.add_file(trainer.checkpoint_callback.best_model_path)
        else:
            for path in Path(self.ckpt_dir).rglob("*.ckpt"):
                ckpts.add_file(str(path))

        experiment.log_artifact(ckpts)

NUM_INPUT_FRAMES = 4
NUM_TARGET_FRAMES = 18

features = datasets.Features(
                {
                    "input": datasets.Array4D(shape=(4,256,256,1), dtype="float32"),
                    "output": datasets.Array4D(shape=(18,256,256,1), dtype="float32"),
                    "mask": datasets.Array4D(shape=(18,256,256,1), dtype="bool"),
                    }
                )

def extract_input_and_target_frames(radar_frames):
    """Extract input and target frames from a dataset row's radar_frames."""
    # We align our targets to the end of the window, and inputs precede targets.
    input_frames = radar_frames[-NUM_TARGET_FRAMES-NUM_INPUT_FRAMES : -NUM_TARGET_FRAMES]
    target_frames = radar_frames[-NUM_TARGET_FRAMES : ]
    return input_frames, target_frames

def process_data(example):
    input_frames, target_frames = extract_input_and_target_frames(example["radar_frames"])
    return {"input": np.moveaxis(input_frames, [0, 1, 2, 3], [0, 2, 3, 1]), "target": np.moveaxis(target_frames, [0, 1, 2, 3], [0, 2, 3, 1]),
            "mask": np.moveaxis(example["radar_mask"][-NUM_TARGET_FRAMES : ], [0, 1, 2, 3], [0, 2, 3, 1]),}

class DGMRDataModule(LightningDataModule):
    """
    Example of LightningDataModule for NETCDF dataset.
    A DataModule implements 5 key methods:
        - prepare_data (things to do on 1 GPU/TPU, not on every GPU/TPU in distributed mode)
        - setup (things to do on every accelerator in distributed mode)
        - train_dataloader (the training dataloader)
        - val_dataloader (the validation dataloader(s))
        - test_dataloader (the test dataloader(s))
    This allows you to share a full dataset without explaining how to download,
    split, transform and process the data.
    Read the docs:
        https://pytorch-lightning.readthedocs.io/en/latest/extensions/datamodules.html
    """

    def __init__(
        self,
        num_workers: int = 1,
        pin_memory: bool = True,
    ):
        """
        fake_data: random data is created and used instead. This is useful for testing
        """
        super().__init__()

        self.num_workers = num_workers
        self.pin_memory = pin_memory

        self.dataloader_config = dict(
            pin_memory=self.pin_memory,
            num_workers=self.num_workers,
            prefetch_factor=8,
            persistent_workers=True,
            # Disable automatic batching because dataset
            # returns complete batches.
            batch_size=None,
        )

    def train_dataloader(self):
        train_dset = datasets.load_dataset("openclimatefix/nimrod-uk-1km", "crops", split="train", streaming=True)
        train_dset = train_dset.map(process_data, features=features, remove_columns=train_dset.column_names).with_format("torch")
        dataloader = DataLoader(train_dset, batch_size=12, num_workers=1)
        return dataloader

    def val_dataloader(self):
        val_dset = datasets.load_dataset("openclimatefix/nimrod-uk-1km", "crops", split="validation", streaming=True)
        val_dset = val_dset.map(process_data, features=features,
                                    remove_columns=val_dset.column_names).with_format("torch")
        dataloader = DataLoader(val_dset, batch_size=6, num_workers=1)
        return dataloader


wandb_logger = WandbLogger(logger="dgmr")
model_checkpoint = ModelCheckpoint(
    monitor="val/g_loss",
    dirpath="./",
    filename="best",
)

trainer = Trainer(
    max_epochs=1000,
    logger=wandb_logger,
    callbacks=[model_checkpoint],
    gpus=6,
    precision=32,
    #accelerator="tpu", devices=8
)
model = DGMR()
datamodule = DGMRDataModule()
trainer.fit(model, datamodule)

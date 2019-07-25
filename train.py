"""
Training module.
"""

import argparse
import glob
import logging
import os
import os.path as osp
import warnings
from shutil import rmtree

import torch
import numpy as np
from tqdm import tqdm

from models import initialize_model
from utils import record
from utils import underline
from utils.metrics import accuracy
from utils.metrics import dice
from utils.metrics import detection_f1
from utils.metrics import object_dice
from utils.history import HistoryTracker

warnings.filterwarnings('ignore')

# training logger
logger = logging.getLogger('Train')
logger.setLevel(logging.DEBUG)

# which device to use
device = None

# Train/val dataLoaders dictionary
dataloaders = None

# path to experiment record directory
record_dir = None

# history metrics tracker object
tracker = None


def build_cli_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset_path', help='Path to dataset')
    parser.add_argument('-d', '--device', default=('cuda' if torch.cuda.is_available() else 'cpu'),
                        help='Which device to use')
    parser.add_argument('-m', '--model', default='wessup',
                        help='Which model to use')
    parser.add_argument('-e', '--epochs', type=int, default=100,
                        help='Number of training epochs')
    parser.add_argument('-b', '--batch-size', type=int, default=1, help='Minibatch size')
    parser.add_argument('--proportion', type=float, default=1.0,
                        help='Proportion of data used for training')
    parser.add_argument('-r', '--resume-ckpt',
                        help='Path to previous checkpoint for resuming training')
    parser.add_argument('--message', help='Note on this experiment')
    parser.add_argument('--smoke', action='store_true', default=False,
                        help='Whether this is a smoke test')

    return parser


def train_one_iteration(model, optimizer, phase, *data):
    input_, target = model.preprocess(*data, device=device)

    optimizer.zero_grad()
    metrics = dict()

    with torch.set_grad_enabled(phase == 'train'):
        pred = model(input_)
        if phase == 'train':
            loss = model.compute_loss(pred, target, metrics=metrics)
            metrics['loss'] = loss.item()

            loss.backward()
            optimizer.step()

    metric_funcs = [accuracy, dice]
    if phase == 'val':
        metric_funcs.extend([detection_f1, object_dice])

    pred, target = model.postprocess(pred, target)
    tracker.step({**metrics, **model.evaluate(pred, target, metric_funcs)})


def train_one_epoch(model, optimizer, no_val=False):
    phases = ['train'] if no_val else ['train', 'val']
    for phase in phases:
        logger.info(f'{phase.capitalize()} phase:')

        if phase == 'train':
            model.train()
            tracker.train()
        else:
            model.eval()
            tracker.eval()

        pbar = tqdm(dataloaders[phase])
        for data in pbar:
            try:
                train_one_iteration(model, optimizer, phase, *data)
            except RuntimeError as ex:
                logger.warning(ex)

        logger.info(tracker.log())
        pbar.close()


def fit(args):
    ############################# MODEL #############################
    checkpoint = None
    if args.resume_ckpt is not None:
        logger.info(f'Loading checkpoints from {args.resume_ckpt}.')
        checkpoint = torch.load(args.resume_ckpt, map_location=device)

    model = initialize_model(args.model, checkpoint=checkpoint)
    model.summary(logger=logger)
    model = model.to(device)
    record.save_params(record_dir,
                       {**vars(args), 'model_config': model.config._to_dict()})

    ############################# DATA #############################
    train_path = osp.join(args.dataset_path, 'train')
    val_path = osp.join(args.dataset_path, 'val')

    global dataloaders
    train_dataset = model.get_default_dataset(train_path, proportion=args.proportion)
    train_dataset.summary(logger=logger)
    dataloaders = {
        'train': torch.utils.data.DataLoader(
            train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0,)
    }

    if osp.exists(val_path):
        val_dataset = model.get_default_dataset(val_path, train=False)
        val_dataset.summary(logger=logger)
        dataloaders['val'] = torch.utils.data.DataLoader(
            val_dataset, batch_size=1, num_workers=0,)

    ############################# TRAIN #############################
    logger.info(underline('\nTraining Stage', '='))
    optimizer, scheduler = model.get_default_optimizer(checkpoint)
    initial_epoch = checkpoint['epoch'] + 1 if checkpoint is not None else 1
    total_epochs = args.epochs + initial_epoch - 1

    for epoch in range(initial_epoch, total_epochs + 1):
        logger.info(underline('\nEpoch {}/{}'.format(epoch, total_epochs), '-'))

        tracker.start_new_epoch(optimizer.param_groups[0]['lr'])
        train_one_epoch(model, optimizer, no_val=(not osp.exists(val_path)))

        if scheduler is not None:
            labeled_loss = np.mean(tracker.history['loss'])

            # only adjust learning rate according to loss of labeled examples
            if 'propagate_loss' in tracker.history:
                labeled_loss -= np.mean(tracker.history['propagate_loss'])

            scheduler.step(labeled_loss)

        # save metrics to csv file
        tracker.save()

        # save learning curves
        record.plot_learning_curves(tracker.save_path)

        # remove previous checkpoints
        for ckpt in glob.glob(osp.join(record_dir, 'checkpoints', '*.pth')):
            os.remove(ckpt)

        # save checkpoints for resuming training
        ckpt_path = osp.join(
            record_dir, 'checkpoints', 'ckpt.{:04d}.pth'.format(epoch))
        model.save_checkpoint(ckpt_path, epoch=epoch,
                              optimizer_state_dict=optimizer.state_dict())

    tracker.report()


if __name__ == '__main__':
    parser = build_cli_parser()
    args = parser.parse_args()

    device = args.device

    if args.resume_ckpt is not None:
        record_dir = osp.dirname(osp.dirname(args.resume_ckpt))
    else:
        record_dir = record.prepare_record_dir()
        record.copy_source_files(record_dir)

    tracker = HistoryTracker(osp.join(record_dir, 'history.csv'))

    logger.addHandler(logging.StreamHandler())
    logger.addHandler(logging.FileHandler(osp.join(record_dir, 'train.log')))

    try:
        fit(args)
    finally:
        if args.smoke:
            rmtree(record_dir, ignore_errors=True)

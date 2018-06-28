import time
import random
import os
import cv2
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import argparse

import mxnet as mx
from mxnet.contrib.ndarray import MultiBoxPrior, MultiBoxTarget, MultiBoxDetection, box_nms
import numpy as np
from skimage.draw import line_aa
from skimage import transform as skimage_tf

from mxnet import nd, autograd, gluon
from mxnet.image import resize_short
from mxboard import SummaryWriter
from mxnet.gluon.model_zoo.vision import resnet34_v1
np.seterr(all='raise')

import multiprocessing

GPU_COUNT = 4
ctx = [mx.gpu(i) for i in range(GPU_COUNT)]
mx.random.seed(1)

from utils.iam_dataset import IAMDataset
from utils.draw_box_on_image import draw_boxes_on_image

print_every_n = 5
send_image_every_n = 20
save_every_n = 50

# Last run python line_segmentation.py --min_c 0.01 --overlap_thres 0.10 --topk 150 --epoch 751 --checkpoint_name ssd_750.params

# To run:
#     python line_segmentation.py --min_c 0.01 --overlap_thres 0.10 --topk 150 --epoch 751 --checkpoint_name ssd_750.params
# For fine_tuning:
#    python line_segmentation.py -p ssd_550.params 

def make_cnn():
    '''
    Create the feature extraction network of the SSD based on resnet34.
    '''
    pretrained = resnet34_v1(pretrained=True, ctx=ctx)
    pretrained_2 = resnet34_v1(pretrained=True, ctx=mx.cpu(0))
    first_weights = pretrained_2.features[0].weight.data().mean(axis=1).expand_dims(axis=1)
    
    body = gluon.nn.HybridSequential()
    with body.name_scope():
        first_layer = gluon.nn.Conv2D(channels=64, kernel_size=(7, 7), padding=(3, 3), strides=(2, 2), in_channels=1, use_bias=False)
        first_layer.initialize(mx.init.Normal(), ctx=ctx)
        first_layer.weight.set_data(first_weights)
        body.add(first_layer)
        body.add(*pretrained.features[1:-3])
        body.hybridize()
    return body

def class_predictor(num_anchors, num_classes):
    '''
    Creates the category prediction network (takes input from each downsampled feature)
    '''
    return gluon.nn.Conv2D(num_anchors * (num_classes + 1), 3, padding=1)

def box_predictor(num_anchors):
    '''
    Creates the bounding box prediction network (takes input from each downsampled feature)
    '''
    pred = gluon.nn.HybridSequential()
    with pred.name_scope():
        pred.add(gluon.nn.Conv2D(channels=num_anchors * 4, kernel_size=(3, 3), padding=1))
        pred.add(gluon.nn.BatchNorm())

        pred.add(gluon.nn.Conv2D(channels=num_anchors * 4, kernel_size=3, padding=1))
        pred.add(gluon.nn.BatchNorm())

        pred.add(gluon.nn.Conv2D(channels=num_anchors * 4, kernel_size=3, padding=1))
        pred.add(gluon.nn.BatchNorm())

        pred.add(gluon.nn.Conv2D(channels=num_anchors * 4, kernel_size=3, padding=1))

    pred.hybridize()
    return pred

def down_sample(num_filters):
    '''
    Creates a two-stacked Conv-BatchNorm-Relu and then a pooling layer to
    downsample the image features by half.
    '''
    out = gluon.nn.HybridSequential()
    for _ in range(2):
        out.add(gluon.nn.Conv2D(num_filters, 3, strides=1, padding=1))
        out.add(gluon.nn.BatchNorm(in_channels=num_filters))
        out.add(gluon.nn.Activation('relu'))
    out.add(gluon.nn.MaxPool2D(2))
    return out

def flatten_prediction(pred):
    '''
    Helper function to flatten the predicted bounding boxes and categories
    '''
    return nd.flatten(nd.transpose(pred, axes=(0, 2, 3, 1)))

def concat_predictions(preds):
    '''
    Helper function to concatenate the predicted bounding boxes and categories
    from different anchor box predictions
    '''
    return nd.concat(*preds, dim=1)

class SSD(gluon.Block):
    def __init__(self, num_classes, **kwargs):
        super(SSD, self).__init__(**kwargs)
        self.anchor_sizes = [[.75, .79], [.79, .84], [.81, .85], [.85, .89], [.88, .961]] #TODO: maybe reduce the number of types boxes?
        self.anchor_ratios = [[10, 8, 6], [9, 7, 5], [7, 5, 3], [6, 4, 2], [5, 3, 1]] 
        self.num_classes = num_classes

        with self.name_scope():
            self.body, self.downsamples, self.class_preds, self.box_preds = self.get_ssd_model(4, num_classes)
            self.downsamples.initialize(mx.init.Normal(), ctx=ctx)
            self.class_preds.initialize(mx.init.Normal(), ctx=ctx)
            self.box_preds.initialize(mx.init.Normal(), ctx=ctx)

    def get_ssd_model(self, num_anchors, num_classes):
        '''
        Creates the SSD model that includes the image feature, downsample, category
        and bounding boxes prediction networks.
        '''
        body = make_cnn()
        downsamples = gluon.nn.Sequential()
        class_preds = gluon.nn.Sequential()
        box_preds = gluon.nn.Sequential()

        downsamples.add(down_sample(128))
        downsamples.add(down_sample(128))
        downsamples.add(down_sample(128))

        for scale in range(5):
            class_preds.add(class_predictor(num_anchors, num_classes))
            box_preds.add(box_predictor(num_anchors))

        return body, downsamples, class_preds, box_preds

    def ssd_forward(self, x):
        '''
        Helper function of the forward pass of the sdd
        '''
        x = self.body(x)

        default_anchors = []
        predicted_boxes = []
        predicted_classes = []

        for i in range(5):
            default_anchors.append(MultiBoxPrior(x, sizes=self.anchor_sizes[i], ratios=self.anchor_ratios[i]))
            predicted_boxes.append(flatten_prediction(self.box_preds[i](x)))
            predicted_classes.append(flatten_prediction(self.class_preds[i](x)))
            if i < 3:
                x = self.downsamples[i](x)
            elif i == 3:
                x = nd.Pooling(x, global_pool=True, pool_type='max', kernel=(4, 4))

        return default_anchors, predicted_classes, predicted_boxes

    def forward(self, x):
        default_anchors, predicted_classes, predicted_boxes = self.ssd_forward(x)
        # we want to concatenate anchors, class predictions, box predictions from different layers
        anchors = concat_predictions(default_anchors)
        box_preds = concat_predictions(predicted_boxes)
        class_preds = concat_predictions(predicted_classes)
        class_preds = nd.reshape(class_preds, shape=(0, -1, self.num_classes + 1))
        return anchors, class_preds, box_preds
    
def training_targets(default_anchors, class_predicts, labels):
    '''
    Helper function to obtain the bounding boxes from the anchors.
    '''
    class_predicts = nd.transpose(class_predicts, axes=(0, 2, 1))
    box_target, box_mask, cls_target = MultiBoxTarget(default_anchors, labels, class_predicts)
    return box_target, box_mask, cls_target

class SmoothL1Loss(gluon.loss.Loss):
    '''
    A SmoothL1loss function defined in https://gluon.mxnet.io/chapter08_computer-vision/object-detection.html
    '''
    def __init__(self, batch_axis=0, **kwargs):
        super(SmoothL1Loss, self).__init__(None, batch_axis, **kwargs)

    def hybrid_forward(self, F, output, label, mask):
        loss = F.smooth_l1((output - label) * mask, scalar=1.0)
        return F.mean(loss, self._batch_axis, exclude=True)

def augment_transform(image, label):
    '''
    1) Function that randomly translates the input image by +-width_range and +-height_range.
    The labels (bounding boxes) are also translated by the same amount.
    2) Each line can also be randomly removed for augmentation. Labels are also reduced to correspond to this
    data and label are converted into tensors by calling the "transform" function.
    '''
    ty = random.uniform(-random_y_translation, random_y_translation)
    tx = random.uniform(-random_x_translation, random_x_translation)

    st = skimage_tf.SimilarityTransform(translation=(tx*image.shape[1], ty*image.shape[0]))
    image = skimage_tf.warp(image, st, cval=1.0)

    label[:, 0] = label[:, 0] - tx/2 #NOTE: Check why it has to be halfed (found experimentally)
    label[:, 1] = label[:, 1] - ty/2
    
    index = np.random.uniform(0, 1.0, size=label.shape[0]) > random_remove_box
    for i, should_output_bb in enumerate(index):
        if should_output_bb == False:
            (x, y, w, h) = label[i]
            (x1, y1, x2, y2) = (x, y, x + w, y + h)
            (x1, y1, x2, y2) = (x1 * image.shape[1], y1 * image.shape[0],
                                x2 * image.shape[1], y2 * image.shape[0])
            (x1, y1, x2, y2) = (int(x1), int(y1), int(x2), int(y2))
            x1 = 0 if x1 < 0 else x1
            y1 = 0 if y1 < 0 else y1
            image_h, image_w = image.shape
            x2 = image_w if x2 > image_w else x2
            y2 = image_h if y2 > image_h else y2

            mean_value = 1.0 #np.mean(data[y1:y2, x1:x2])
            image[y1:y2, x1:x2] = mean_value
                
    augmented_labels = label[index, :]
    return transform(image*255., augmented_labels)

def transform(image, label):
    '''
    Function that converts resizes image into the input image tensor for a CNN.
    The labels (bounding boxes) are expanded, converted into (x, y, x+w, y+h), and
    zero padded to the maximum number of labels. Finally, it is converted into a float
    tensor.
    '''
    max_label_n = 13

    # Resize the image
    image = np.expand_dims(image, axis=2)
    image = mx.nd.array(image)
    image = resize_short(image, int(700/2))
    image = image.transpose([2, 0, 1])/255.

    # Expand the bounding box by expand_bb_scale
    bb = label
    new_w = (1 + expand_bb_scale) * bb[:, 2]
    new_h = (1 + expand_bb_scale) * bb[:, 3]
    
    bb[:, 0] = bb[:, 0] - (new_w - bb[:, 2])/2
    bb[:, 1] = bb[:, 1] - (new_h - bb[:, 3])/2
    bb[:, 2] = new_w
    bb[:, 3] = new_h
    label = bb 

    # Convert the predicted bounding box from (x, y, w, h to (x, y, x + w, y + h)
    label = label.astype(np.float32)
    label[:, 2] = label[:, 0] + label[:, 2]
    label[:, 3] = label[:, 1] + label[:, 3]

    # Zero pad the data
    label_n = label.shape[0]
    label_padded = np.zeros(shape=(max_label_n, 5))
    label_padded[:label_n, 1:] = label
    label_padded[:label_n, 0] = np.ones(shape=(1, label_n))
    label_padded = mx.nd.array(label_padded)
    
    return image, label_padded

def run_epoch(e, network, dataloader, trainer, log_dir, print_name, update_cnn, update_metric, save_cnn):
    total_losses = [nd.zeros(1, ctx_i) for ctx_i in ctx]
    for i, (X, Y) in enumerate(dataloader):
        X = gluon.utils.split_and_load(X, ctx)
        Y = gluon.utils.split_and_load(Y, ctx)
        
        with autograd.record():
            losses = []
            for x, y in zip(X, Y):
                default_anchors, class_predictions, box_predictions = network(x)
                box_target, box_mask, cls_target = training_targets(default_anchors, class_predictions, y)
                # losses
                loss_class = cls_loss(class_predictions, cls_target)
                loss_box = box_loss(box_predictions, box_target, box_mask)
                # sum all losses
                loss = loss_class + loss_box
                losses.append(loss)
            
        if update_cnn:
            for loss in losses:
                loss.backward()
            step_size = 0
            for x in X:
                step_size += x.shape[0]
            trainer.step(step_size)

        for index, loss in enumerate(losses):
            total_losses[index] += loss.mean()/len(ctx)
            
        if update_metric:
            cls_metric.update([cls_target], [nd.transpose(class_predictions, (0, 2, 1))])
            box_metric.update([box_target], [box_predictions * box_mask])
            
        if i == 0 and e % send_image_every_n == 0 and e > 0:
            cls_probs = nd.SoftmaxActivation(nd.transpose(class_predictions, (0, 2, 1)), mode='channel')
            output = MultiBoxDetection(*[cls_probs, box_predictions, default_anchors], force_suppress=True, clip=False)
            output = box_nms(output, overlap_thresh=overlap_thres, valid_thresh=min_c, topk=topk)
            output = output.asnumpy()

            number_of_bbs = 0
            predicted_bb = []
            for b in range(output.shape[0]):
                predicted_bb_ = output[b, output[b, :, 0] != -1]
                predicted_bb_ = predicted_bb_[:, 2:]
                number_of_bbs += predicted_bb_.shape[0]
                predicted_bb_[:, 2] = predicted_bb_[:, 2] - predicted_bb_[:, 0]
                predicted_bb_[:, 3] = predicted_bb_[:, 3] - predicted_bb_[:, 1]
                predicted_bb.append(predicted_bb_)
            labels = y[:, :, 1:].asnumpy()
            labels[:, :, 2] = labels[:, :, 2] - labels[:, :, 0]
            labels[:, :, 3] = labels[:, :, 3] - labels[:, :, 1]

            with SummaryWriter(logdir=log_dir, verbose=False, flush_secs=5) as sw:
                output_image = draw_boxes_on_image(predicted_bb, labels, x.asnumpy())
                output_image[output_image<0] = 0
                output_image[output_image>1] = 1
                print("Number of predicted {} BBs = {}".format(print_name, number_of_bbs))
                sw.add_image('bb_{}_image'.format(print_name), output_image, global_step=e)

    total_loss = 0
    for loss in total_losses:
        total_loss = loss.asscalar()
    epoch_loss = float(total_loss)/len(dataloader)

    with SummaryWriter(logdir=log_dir, verbose=False, flush_secs=5) as sw:
        if update_metric:
            name1, val1 = cls_metric.get()
            name2, val2 = box_metric.get()
            sw.add_scalar(name1, {"test": val1}, global_step=e)
            sw.add_scalar(name2, {"test": val2}, global_step=e)
        sw.add_scalar('loss', {print_name: epoch_loss}, global_step=e)
            
    if save_cnn and e % save_every_n == 0 and e > 0:
        network.save_parameters("{}/{}".format(checkpoint_dir, checkpoint_name))
    return epoch_loss

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("-b", "--expand_bb_scale", default=0.05,
                        help="Scale to expand the bounding box")
    parser.add_argument("-m", "--min_c", default=0.01,
                        help="Minimum probability to be considered a bounding box (used in box_nms)")
    parser.add_argument("-o", "--overlap_thres", default=0.2,
                        help="Maximum overlap between bounding boxes")
    parser.add_argument("-t", "--topk", default=20,
                        help="Maximum number of bounding boxes on one slide")
    
    parser.add_argument("-e", "--epochs", default=751,
                        help="Number of epochs to run")
    parser.add_argument("-l", "--learning_rate", default=0.0001,
                        help="Learning rate for training")
    parser.add_argument("-s", "--batch_size", default=32,
                        help="Batch size")

    parser.add_argument("-x", "--random_x_translation", default=0.03,
                        help="Randomly translation the image in the x direction (+ or -)")
    parser.add_argument("-y", "--random_y_translation", default=0.03,
                        help="Randomly translation the image in the y direction (+ or -)")
    parser.add_argument("-r", "--random_remove_box", default=0.15,
                        help="Randomly remove bounding boxes and texts with a probability of r")
    
    parser.add_argument("-d", "--log_dir", default="./logs",
                        help="Directory to store the log files")
    parser.add_argument("-c", "--checkpoint_dir", default="model_checkpoint",
                        help="Directory to store the checkpoints")
    parser.add_argument("-n", "--checkpoint_name", default="ssd.params",
                        help="Name to store the checkpoints")
    parser.add_argument("-p", "--load_model", default=None,
                        help="Model to load from")

    args = parser.parse_args()
    expand_bb_scale = float(args.expand_bb_scale)
    min_c = float(args.min_c)
    overlap_thres = float(args.overlap_thres)
    topk = int(args.topk)
    
    epochs = int(args.epochs)
    learning_rate = float(args.learning_rate)
    batch_size = int(args.batch_size) * len(ctx)

    random_y_translation, random_x_translation = float(args.random_x_translation), float(args.random_y_translation)
    random_remove_box = float(args.random_remove_box)

    log_dir = args.log_dir
    checkpoint_dir, checkpoint_name = args.checkpoint_dir, args.checkpoint_name
    load_model = args.load_model

    train_ds = IAMDataset("form_bb", output_data="bb", output_parse_method="line", train=True)
    print("Number of training samples: {}".format(len(train_ds)))

    test_ds = IAMDataset("form_bb", output_data="bb", output_parse_method="line", train=False)
    print("Number of testing samples: {}".format(len(test_ds)))

    train_data = gluon.data.DataLoader(train_ds.transform(augment_transform), batch_size, shuffle=True, last_batch="discard", num_workers=multiprocessing.cpu_count()-2)
    test_data = gluon.data.DataLoader(test_ds.transform(transform), batch_size, shuffle=False, last_batch="discard", num_workers=multiprocessing.cpu_count()-2)

    net = SSD(2)
    if load_model is not None:
        net.load_parameters("{}/{}".format(checkpoint_dir, load_model))

    trainer = gluon.Trainer(net.collect_params(), 'adam', {'learning_rate': learning_rate, })
    
    cls_loss = gluon.loss.SoftmaxCrossEntropyLoss()

    box_loss = SmoothL1Loss()
    cls_metric = mx.metric.Accuracy()
    box_metric = mx.metric.MAE()

    for e in range(epochs):
        cls_metric.reset()
        box_metric.reset()

        train_loss = run_epoch(e, net, train_data, trainer, log_dir, print_name="train", update_cnn=True, update_metric=False, save_cnn=True)
        test_loss = run_epoch(e, net, test_data, trainer, log_dir, print_name="test", update_cnn=False, update_metric=True, save_cnn=False)
        if e % print_every_n == 0:
            name1, val1 = cls_metric.get()
            name2, val2 = box_metric.get()
            print("Epoch {0}, train_loss {1:.6f}, test_loss {2:.6f}, test {3}={4:.6f}, {5}={6:.6f}".format(e, train_loss, test_loss,
                                                                                                           name1, val1, name2, val2))

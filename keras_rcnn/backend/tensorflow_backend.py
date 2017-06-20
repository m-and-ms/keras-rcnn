import itertools
import keras.backend
import numpy
import tensorflow
import keras_rcnn.backend


RPN_NEGATIVE_OVERLAP = 0.3
RPN_POSITIVE_OVERLAP = 0.7
RPN_FG_FRACTION = 0.5
RPN_BATCHSIZE = 256

def bbox_transform_inv(shifted, boxes):
    if boxes.shape[0] == 0:
        return tensorflow.zeros((0, boxes.shape[1]), dtype=boxes.dtype)

    a = shifted[:, 2] - shifted[:, 0] + 1.0
    b = shifted[:, 3] - shifted[:, 1] + 1.0

    ctr_x = shifted[:, 0] + 0.5 * a
    ctr_y = shifted[:, 1] + 0.5 * b

    dx = boxes[:, 0::4]
    dy = boxes[:, 1::4]
    dw = boxes[:, 2::4]
    dh = boxes[:, 3::4]

    pred_ctr_x = dx * a[:, tensorflow.newaxis] + ctr_x[:, tensorflow.newaxis]
    pred_ctr_y = dy * b[:, tensorflow.newaxis] + ctr_y[:, tensorflow.newaxis]

    pred_w = tensorflow.exp(dw) * a[:, tensorflow.newaxis]
    pred_h = tensorflow.exp(dh) * b[:, tensorflow.newaxis]

    pred_boxes = [
        pred_ctr_x - 0.5 * pred_w,
        pred_ctr_y - 0.5 * pred_h,
        pred_ctr_x + 0.5 * pred_w,
        pred_ctr_y + 0.5 * pred_h
    ]

    return keras.backend.concatenate(pred_boxes)


def filter_boxes(proposals, minimum):
    ws = proposals[:, 2] - proposals[:, 0] + 1
    hs = proposals[:, 3] - proposals[:, 1] + 1

    indicies = tensorflow.where((ws >= minimum) & (hs >= minimum))

    indicies = keras.backend.flatten(indicies)

    return keras.backend.cast(indicies, tensorflow.int32)


def non_maximum_suppression(boxes, scores, maximum, threshold=0.5):
    return tensorflow.image.non_max_suppression(
        boxes=boxes,
        iou_threshold=threshold,
        max_output_size=maximum,
        scores=scores
    )


def propose(boxes, scores, maximum):
    shape = keras.backend.int_shape(boxes)[1:3]

    shifted = keras_rcnn.backend.shift(shape, 16)

    proposals = keras.backend.reshape(boxes, (-1, 4))

    proposals = keras_rcnn.backend.bbox_transform_inv(shifted, proposals)

    proposals = keras_rcnn.backend.clip(proposals, shape)

    indicies = keras_rcnn.backend.filter_boxes(proposals, 1)

    proposals = keras.backend.gather(proposals, indicies)

    scores = scores[:, :, :, :9]
    scores = keras.backend.reshape(scores, (-1, 1))
    scores = keras.backend.gather(scores, indicies)
    scores = keras.backend.flatten(scores)

    proposals = keras.backend.cast(proposals, tensorflow.float32)
    scores = keras.backend.cast(scores, tensorflow.float32)

    indicies = keras_rcnn.backend.non_maximum_suppression(proposals, scores, maximum, 0.7)

    proposals = keras.backend.gather(proposals, indicies)

    return keras.backend.expand_dims(proposals, 0)


def resize_images(images, shape):
    return tensorflow.image.resize_images(images, shape)


def crop_and_resize(image, boxes, size):
    """Crop the image given boxes and resize with bilinear interplotation.
    # Parameters
    image: Input image of shape (1, image_height, image_width, depth)
    boxes: Regions of interest of shape (1, num_boxes, 4),
    each row [y1, x1, y2, x2]
    size: Fixed size [h, w], e.g. [7, 7], for the output slices.
    # Returns
    4D Tensor (number of regions, slice_height, slice_width, channels)
    """
    box_ind = tensorflow.zeros_like(boxes, tensorflow.int32)
    box_ind = box_ind[..., 0]
    box_ind = tensorflow.reshape(box_ind, [-1])

    boxes = tensorflow.reshape(boxes, [-1, 4])
    return tensorflow.image.crop_and_resize(image, boxes, box_ind, size)


def overlap(a, b):
    """
    Parameters
    ----------
    a: (N, 4) ndarray of float
    b: (K, 4) ndarray of float
    Returns
    -------
    overlaps: (N, K) ndarray of overlap between boxes and query_boxes
    """

    overlaps = numpy.zeros((a.shape[0], b.shape[0]), dtype=numpy.float)

    for k, n in itertools.product(range(b.shape[0]), range(a.shape[0])):
        area = ((b[k, 2] - b[k, 0] + 1) * (b[k, 3] - b[k, 1] + 1))

        iw = (min(a[n, 2], b[k, 2]) - max(a[n, 0], b[k, 0]) + 1)

        if iw > 0:
            ih = (min(a[n, 3], b[k, 3]) - max(a[n, 1], b[k, 1]) + 1)

            if ih > 0:
                ua = float((a[n, 2] - a[n, 0] + 1) * (a[n, 3] - a[n, 1] + 1) + area - iw * ih)

                overlaps[n, k] = iw * ih / ua

    return overlaps


def overlapping(y_true, y_pred, inds_inside):
    """
    overlaps between the anchors and the gt boxes

    :param y_pred: anchors
    :param y_true:
    :param inds_inside:

    :return:
    """
    overlaps = overlap(y_pred, y_true[:, :4])

    argmax_overlaps_inds = overlaps.argmax(axis=1)
    gt_argmax_overlaps_inds = overlaps.argmax(axis=0)

    max_overlaps = overlaps[numpy.arange(len(inds_inside)),argmax_overlaps_inds]#overlaps[keras.backend.arange(len(inds_inside)), argmax_overlaps_inds]

    return argmax_overlaps_inds, max_overlaps, gt_argmax_overlaps_inds


def balance(labels):
    """
    balance labels by setting some to -1
    :param labels: array of labels (1 is positive, 0 is negative, -1 is dont care)

    :return: array of labels
    """
    # subsample positive labels if we have too many
    labels = subsample_positive_labels(labels)

    # subsample negative labels if we have too many
    labels = subsample_negative_labels(labels)

    return labels


def subsample_positive_labels(labels):
    """
    subsample positive labels if we have too many
    :param labels: array of labels (1 is positive, 0 is negative, -1 is dont care)

    :return:
    """
    num_fg = int(RPN_FG_FRACTION * RPN_BATCHSIZE)

    fg_inds = numpy.where(labels == 1)[0]# tensorflow.where(labels == 1)[0]
    '''
    if len(fg_inds) > num_fg:
        size = int(len(fg_inds) - num_fg)

        elems = tensorflow.gather(fg_inds, tensorflow.multinomial(tensorflow.ones(len(fg_inds))[:, tensorflow.newaxis], size))
        numpy.random.choice(fg_inds, size, replace=False)
        labels[elems] = -1 
    '''
    if len(fg_inds) > num_fg:
        size = int(len(fg_inds) - num_fg)

        labels[numpy.random.choice(fg_inds, size, replace=False)] = -1

    return labels


def subsample_negative_labels(labels):
    """
    subsample negative labels if we have too many
    :param labels: array of labels (1 is positive, 0 is negative, -1 is dont care)

    :return:
    """
    num_bg = RPN_BATCHSIZE - numpy.sum(labels[labels == 1])# tensorflow.reduce_sum(labels[labels == 1])

    bg_inds = numpy.where(labels == 0)[0] #tensorflow.where(labels == 0)[0]
    '''
    if len(bg_inds) > num_bg:
        size = bg_inds.shape[0] - num_bg

        elems = tensorflow.gather(bg_inds, tensorflow.multinomial(tensorflow.ones(len(bg_inds))[:, tensorflow.newaxis], size))
        numpy.random.choice(bg_inds, size, replace=False)
        labels[elems] = -1
    '''
    if len(bg_inds) > num_bg:
        size = int(len(bg_inds) - num_bg)

        labels[numpy.random.choice(bg_inds, size, replace=False)] = -1

    return labels


def label(y_true, y_pred, inds_inside):
    """
    Create bbox labels.
    label: 1 is positive, 0 is negative, -1 is dont care

    :param inds_inside:
    :param y_pred: anchors
    :param y_true:

    :return:
    """
    # assign ignore labels first
    labels = numpy.ones((len(inds_inside),),) * -1 # tensorflow.ones((len(inds_inside),), dtype=tensorflow.int32) * -1

    argmax_overlaps_inds, max_overlaps, gt_argmax_overlaps_inds = overlapping(y_true, y_pred, inds_inside)

    # assign bg labels first so that positive labels can clobber them
    labels[max_overlaps < RPN_NEGATIVE_OVERLAP] = 0

    # fg label: for each gt, anchor with highest overlap
    labels[gt_argmax_overlaps_inds] = 1

    # fg label: above threshold IOU
    labels[max_overlaps >= RPN_POSITIVE_OVERLAP] = 1

    # assign bg labels last so that negative labels can clobber positives
    labels[max_overlaps < RPN_NEGATIVE_OVERLAP] = 0

    labels = balance(labels)

    return argmax_overlaps_inds, labels


def shift(shape, stride):

    shift_x = numpy.arange(0, shape[0]) * stride#keras.backend.arange(0, shape[0]) * stride
    shift_y = numpy.arange(0, shape[1]) * stride#keras.backend.arange(0, shape[1]) * stride

    shift_x, shift_y = numpy.meshgrid(shift_x, shift_y) #tensorflow.meshgrid(shift_x, shift_y)

    #shifts = tensorflow.concat((tensorflow.reshape(shift_x, [-1]), tensorflow.reshape(shift_y, [-1]), tensorflow.reshape(shift_x, [-1]), tensorflow.reshape(shift_y, [-1])), axis = 0)
    #shifts = tensorflow.transpose(shifts)
    shifts = numpy.vstack((shift_x.ravel(), shift_y.ravel(), shift_x.ravel(), shift_y.ravel())).transpose()
    anchors = keras_rcnn.backend.anchor()

    # Create all bbox
    number_of_anchors = anchors.shape[0]

    k = shifts.shape[0]  # number of base points = feat_h * feat_w

    bbox = anchors.reshape(1, number_of_anchors, 4) + shifts.reshape(k, 1, 4) #tensorflow.reshape(anchors, [1, number_of_anchors, 4]) + tensorflow.reshape(shifts, [k, 1, 4])

    bbox = bbox.reshape(k * number_of_anchors, 4) #tensorflow.reshape(bbox, [k * number_of_anchors, 4])

    return bbox


def inside_image(y_pred, img_info):
    """
    Calc indicies of anchors which are located completely inside of the image
    whose size is specified by img_info ((height, width, scale)-shaped array).

    :param y_pred: anchors
    :param img_info:

    :return:
    """
    inds_inside = numpy.where(#tensorflow.where(
        (y_pred[:, 0] >= 0) &
        (y_pred[:, 1] >= 0) &
        (y_pred[:, 2] < img_info[1]) &  # width
        (y_pred[:, 3] < img_info[0])  # height
    )[0]

    return inds_inside, y_pred[inds_inside]


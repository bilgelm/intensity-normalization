#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
intensity_normalization.normalize.whitestripe

Use the White Stripe method outlined in [1] to normalize
the intensity of an MR image

References:
﻿   [1] R. T. Shinohara, E. M. Sweeney, J. Goldsmith, N. Shiee,
        F. J. Mateen, P. A. Calabresi, S. Jarso, D. L. Pham,
        D. S. Reich, and C. M. Crainiceanu, “Statistical normalization
        techniques for magnetic resonance imaging,” NeuroImage Clin.,
        vol. 6, pp. 9–19, 2014.

Author: Jacob Reinhold (jacob.reinhold@jhu.edu)

Created on: Apr 27, 2018
"""

from __future__ import print_function, division

from glob import glob
import logging
import os

import nibabel as nib
import numpy as np
from pygam import PoissonGAM
from scipy.signal import argrelmax

from intensity_normalization.errors import NormalizationError
from intensity_normalization.utilities import io

logger = logging.getLogger(__name__)


def ws_normalize(img_dir, contrast, mask_dir=None, output_dir=None, write_to_disk=True):
    """
    Use WhiteStripe normalization method ([1]) to normalize the intensities of
    a set of MR images by normalizing an area around the white matter peak of the histogram

    Args:
        img_dir (str): directory containing MR images to be normalized
        contrast (str): contrast of MR images to be normalized (T1, T2, or FLAIR)
        mask_dir (str): if images are not skull-stripped, then provide brain mask
        output_dir (str): directory to save images if you do not want them saved in
            same directory as img_dir
        write_to_disk (bool): write the normalized data to disk or nah

    Returns:
        normalized (np.ndarray): last normalized image data from img_dir
            I know this is an odd behavior, but yolo

    References:
        [1] R. T. Shinohara, E. M. Sweeney, J. Goldsmith, N. Shiee,
            F. J. Mateen, P. A. Calabresi, S. Jarso, D. L. Pham,
            D. S. Reich, and C. M. Crainiceanu, “Statistical normalization
            techniques for magnetic resonance imaging,” NeuroImage Clin.,
            vol. 6, pp. 9–19, 2014.
    """

    # grab the file names for the images of interest
    data = sorted(glob(os.path.join(img_dir, '*.nii*')))

    # define and get the brain masks for the images, if defined
    if mask_dir is None:
        masks = [None] * len(data)
    else:
        masks = sorted(glob(os.path.join(mask_dir, '*.nii*')))
        if len(data) != len(masks):
            NormalizationError('Number of images and masks must be equal, Images: {}, Masks: {}'
                               .format(len(data), len(masks)))

    # define the output directory and corresponding output file names
    if output_dir is None:
        output_files = [None] * len(data)
    else:
        output_files = []
        for fn in data:
            _, base, ext = io.split_filename(fn)
            output_files.append(os.path.join(output_dir, base + ext))
        if not os.path.exists(output_dir):
            os.mkdir(output_dir)

    # control verbosity of output when making whitestripe function call
    verbose = True if logger.getEffectiveLevel() == logging.getLevelName('DEBUG') else False

    # do whitestripe normalization and save the results
    for i, (img_fn, mask_fn, output_fn) in enumerate(zip(data, masks, output_files), 1):
        logger.info('Normalizing image: {} ({:d}/{:d})'.format(img_fn, i, len(data)))
        img = io.open_nii(img_fn)
        mask = io.open_nii(mask_fn) if mask_fn is not None else None
        indices = whitestripe(img, contrast, mask=mask, verbose=verbose)
        normalized = whitestripe_norm(img, indices)
        if write_to_disk:
            logger.info('Saving normalized image: {} ({:d}/{:d})'.format(output_fn, i, len(data)))
            io.save_nii(normalized, output_fn)

    # output the last normalized image (mostly for testing purposes)
    return normalized


def whitestripe(img, contrast, mask=None, nbins=2000, width=0.05, width_l=None, width_u=None, verbose=False):
    """
    find the "(normal appearing) white (matter) stripe" of the input MR image
    and return the indices

    Args:
        img (nibabel.nifti1.Nifti1Image): target MR image
        contrast (str): contrast of img (e.g., T1)
        mask (nibabel.nifti1.Nifti1Image): brainmask for img (None is default, for skull-stripped img)
        nbins (int): number of bins in the histogram
        width (float): width quantile for the "white (matter) stripe"
        width_l (float): lower bound for width (default None, derives from width)
        width_u (float): upper bound for width (default None, derives from width)
        verbose (bool): show progress and warnings or nah

    Returns:
        ws_ind (np.ndarray): the white stripe indices (boolean mask)
    """
    if width_l is None and width_u is None:
        width_l = width
        width_u = width
    img_data = img.get_data()
    if mask is not None:
        mask_data = mask.get_data()
        masked = img_data * mask_data
        voi = img_data[mask_data == 1]
    else:
        masked = img_data
        voi = img_data[img_data > 0]
    counts, bin_edges = np.histogram(voi, bins=nbins)
    bins = np.diff(bin_edges)/2 + bin_edges[:-1]
    if contrast in ['T1', 'FA', 'last']:
        mode = get_last_mode(bins, counts, verbose=verbose)
    elif contrast in ['T2', 'largest']:
        mode = get_largest_mode(bins, counts, verbose=verbose)
    elif contrast in ['MD', 'first']:
        mode = get_first_mode(bins, counts, verbose=verbose)
    else:
        raise NormalizationError('Contrast {} not valid, needs to be T1, T2, FA, or MD')
    img_mode_q = np.mean(voi < mode)
    ws = np.percentile(voi, (max(img_mode_q - width_l, 0) * 100, min(img_mode_q + width_u, 1) * 100))
    ws_ind = np.logical_and(masked > ws[0], masked < ws[1])
    if len(ws_ind) == 0:
        raise NormalizationError('WhiteStripe failed to find any valid indices!')
    return ws_ind


def whitestripe_norm(img, indices):
    """
    use the whitestripe indices to standardize the data (i.e., subtract the
    mean of the values in the indices and divide by the std of those values)

    Args:
        img (nibabel.nifti1.Nifti1Image): target MR image
        indices (np.ndarray): whitestripe indices (see whitestripe func)

    Returns:
        norm_img (nibabel.nifti1.Nifti1Image): normalized image in nifti format
    """
    img_data = img.get_data()
    mu = np.mean(img_data[indices])
    sig = np.std(img_data[indices])
    norm_img_data = (img_data - mu)/sig
    norm_img = nib.Nifti1Image(norm_img_data, img.affine, img.header)
    return norm_img


def get_largest_mode(bins, counts, verbose=False):
    """
    gets the last (reliable) peak in the histogram

    Args:
        bins (np.ndarray): bins of histogram (see np.histogram)
        counts (np.ndarray): counts of histogram (see np.histogram)
        verbose (bool): if true, show progress bar

    Returns:
        largest_peak (int): index of the largest peak
    """
    sh = smooth_hist(bins, counts, verbose=verbose)
    largest_peak = bins[np.argmax(sh)]
    return largest_peak


def get_last_mode(bins, counts, rare_prop=1/5, remove_tail=True, verbose=False):
    """
    gets the last (reliable) peak in the histogram

    Args:
        bins (np.ndarray): bins of histogram (see np.histogram)
        counts (np.ndarray): counts of histogram (see np.histogram)
        rare_prop (float): if remove_tail, use this proportion
        remove_tail (bool): remove rare portions of histogram
            (included to replicate the default behavior in the R version)
        verbose (bool): if true, show progress bar

    Returns:
        last_peak (int): index of the last peak
    """
    if remove_tail:
        which_rare = counts < rare_prop * max(counts)
        counts = counts[which_rare != 1]
        bins = bins[which_rare != 1]
    sh = smooth_hist(bins, counts, verbose=verbose)
    maxima = argrelmax(sh)[0]  # for some reason argrelmax returns a tuple, so [0] extracts value
    last_peak = bins[maxima[-1]]
    return last_peak


def get_first_mode(bins, counts, rare_prop=1/5, remove_tail=True, verbose=False):
    """
    gets the first (reliable) peak in the histogram

    Args:
        bins (np.ndarray): bins of histogram (see np.histogram)
        counts (np.ndarray): counts of histogram (see np.histogram)
        rare_prop (float): if remove_tail, use this proportion
        remove_tail (bool): remove rare portions of histogram
            (included to replicate the default behavior in the R version)
        verbose (bool): if true, show progress bar

    Returns:
        first_peak (int): index of the first peak
    """
    if remove_tail:
        which_rare = counts < rare_prop * max(counts)
        counts = counts[which_rare != 1]
        bins = bins[which_rare != 1]
    sh = smooth_hist(bins, counts, verbose=verbose)
    maxima = argrelmax(sh)[0]  # for some reason argrelmax returns a tuple, so [0] extracts value
    first_peak = bins[maxima[0]]
    return first_peak


def smooth_hist(bins, counts, verbose=False):
    """
    use a generalized additive model to smooth a histogram

    Args:
        bins (np.ndarray): bins of histogram (see np.histogram)
        counts (np.ndarray): counts of histogram (see np.histogram)
        verbose (bool): if true, show progress bar

    Returns:
        smoothed (np.ndarray): smoothed version of counts
    """
    gam = PoissonGAM().gridsearch(bins, counts, progress=verbose)
    smoothed = gam.predict(bins)
    return smoothed

import numpy as np
import cv2
import scipy.ndimage as ndi
import scipy.stats
from skimage.feature import peak_local_max

from skimage.morphology import watershed, disk
from skimage.filters import rank

import caiman as cm
from caiman.motion_correction import tile_and_correct, motion_correction_piecewise
from caiman.source_extraction.cnmf import cnmf as cnmf
from caiman.motion_correction import MotionCorrect
from caiman.components_evaluation import evaluate_components
from caiman.utils.visualization import plot_contours, view_patches_bar
from caiman.base.rois import extract_binary_masks_blob
from caiman.utils.utils import download_demo

import os
import glob

def play_movie(movie):
    t = movie.shape[-1]

    frame_counter = 0
    while True:
        cv2.imshow('Movie', cv2.resize(movie[:, :, frame_counter]/255, (0, 0), fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
        frame_counter += 1
        if frame_counter == t:
            frame_counter = 0
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cv2.destroyAllWindows()

def normalize(image, force=False):
    min_val = np.amin(image)

    image_new = image.copy()

    if min_val < 0:
        image_new -= min_val

    max_val = np.amax(image_new)

    if force:
        return 255*image_new/max_val

    if max_val <= 1:
        return 255*image_new
    elif max_val <= 255:
        return image_new
    else:
        return 255*image_new/max_val

def order_statistic(image, percentile, window_size):
    order = int(np.floor(percentile*window_size**2))
    return ndi.rank_filter(image, order, size=(window_size, window_size))

def rescale_0_1(image):
    (h, w) = image.shape
    n_pixels = h*w
    S = np.reshape(image, (1, n_pixels))
    percent = 5.0/10000
    min_percentile = int(np.maximum(np.floor(n_pixels*percent), 1))
    max_percentile = n_pixels - min_percentile

    denominator = S[0, max_percentile] - S[0, min_percentile]
    if abs(denominator) == 0:
        denominator = 1e-6

    S = np.sort(S)

    return (image - S[0, min_percentile])/denominator

def calculate_local_correlations(movie):
    (h, w, t) = movie.shape

    print(movie.shape)
    cross_correlations = np.zeros((h, w, 2, 2))

    index_range = [-1, 1]

    for i in range(h):
        print(i)
        for j in range(w):
            for k in range(2):
                for l in range(2):
                    ind_1 = index_range[k]
                    ind_2 = index_range[l]
                    cross_correlations[i, j, ind_1, ind_2] = scipy.stats.pearsonr(movie[i, j], movie[min(max(i+ind_1, 0), h-1), min(max(j+ind_2, 0), w-1)])[0]
    
    correlations = np.mean(np.mean(cross_correlations, axis=-1), axis=-1)

    return correlations

def mean(movie):
    return np.mean(movie, axis=-1)

def adjust_contrast(image, contrast):
    return np.minimum(contrast*image, 255)

def adjust_gamma(image, gamma):
    return np.minimum(255*(image/255.0)**(1.0/gamma), 255)

def motion_correct(video_path):
    # --- PARAMETERS --- #

    params_movie = {'fname': video_path,
                     'max_shifts': (6, 6),  # maximum allow rigid shift (2,2)
                     'niter_rig': 3,
                     'splits_rig': 14,  # for parallelization split the movies in  num_splits chuncks across time
                     'num_splits_to_process_rig': None,  # if none all the splits are processed and the movie is saved
                     'strides': (24, 24),  # intervals at which patches are laid out for motion correction
                     'overlaps': (6, 6),  # overlap between pathes (size of patch strides+overlaps)
                     'splits_els': 14,  # for parallelization split the movies in  num_splits chuncks across time
                     'num_splits_to_process_els': [7, None],  # if none all the splits are processed and the movie is saved
                     'upsample_factor_grid': 3,  # upsample factor to avoid smearing when merging patches
                     'max_deviation_rigid': 2,  # maximum deviation allowed for patch with respect to rigid shift         
                     }

    # load movie (in memory!)
    fname = params_movie['fname']
    niter_rig = params_movie['niter_rig']
    # maximum allow rigid shift
    max_shifts = params_movie['max_shifts']  
    # for parallelization split the movies in  num_splits chuncks across time
    splits_rig = params_movie['splits_rig']  
    # if none all the splits are processed and the movie is saved
    num_splits_to_process_rig = params_movie['num_splits_to_process_rig']
    # intervals at which patches are laid out for motion correction
    strides = params_movie['strides']
    # overlap between pathes (size of patch strides+overlaps)
    overlaps = params_movie['overlaps']
    # for parallelization split the movies in  num_splits chuncks across time
    splits_els = params_movie['splits_els'] 
    # if none all the splits are processed and the movie is saved
    num_splits_to_process_els = params_movie['num_splits_to_process_els']
    # upsample factor to avoid smearing when merging patches
    upsample_factor_grid = params_movie['upsample_factor_grid'] 
    # maximum deviation allowed for patch with respect to rigid
    # shift
    max_deviation_rigid = params_movie['max_deviation_rigid']

    # --- RIGID MOTION CORRECTION --- #

    # Load the original movie
    m_orig = cm.load(fname)
    min_mov = np.min(m_orig) # movie must be mostly positive for this to work

    offset_mov = -min_mov

    # Create the cluster
    c, dview, n_processes = cm.cluster.setup_cluster(
        backend='local', n_processes=None, single_thread=False)

    # Create motion correction object
    mc = MotionCorrect(fname, min_mov,
                       dview=dview, max_shifts=max_shifts, niter_rig=niter_rig, splits_rig=splits_rig, 
                       num_splits_to_process_rig=num_splits_to_process_rig, 
                    strides= strides, overlaps= overlaps, splits_els=splits_els,
                    num_splits_to_process_els=num_splits_to_process_els, 
                    upsample_factor_grid=upsample_factor_grid, max_deviation_rigid=max_deviation_rigid, 
                    shifts_opencv = True, nonneg_movie = True)

    # Do rigid motion correction
    mc.motion_correct_rigid(save_movie=True)

    # Load rigid motion corrected movie
    m_rig = cm.load(mc.fname_tot_rig)

    # --- ELASTIC MOTION CORRECTION --- #

    # Do elastic motion correction
    mc.motion_correct_pwrigid(save_movie=True, template=mc.total_template_rig, show_template=False)

    # Save elastic shift border
    bord_px_els = np.ceil(np.maximum(np.max(np.abs(mc.x_shifts_els)),
                                     np.max(np.abs(mc.y_shifts_els)))).astype(np.int)
    np.savez(mc.fname_tot_els + "_bord_px_els.npz", bord_px_els)

    # Load elastic motion corrected movie
    m_els = cm.load(mc.fname_tot_els)

    downsample_factor = 1
    cm.concatenate([m_orig.resize(1, 1, downsample_factor)+offset_mov, m_rig.resize(1, 1, downsample_factor), m_els.resize(
        1, 1, downsample_factor)], axis=2).play(fr=60, gain=5, magnification=0.75, offset=0)

    # Crop elastic shifts out of the movie and save
    fnames = [mc.fname_tot_els]
    border_to_0 = bord_px_els
    idx_x=slice(border_to_0,-border_to_0,None)
    idx_y=slice(border_to_0,-border_to_0,None)
    idx_xy=(idx_x,idx_y)
    # idx_xy = None
    add_to_movie = -np.nanmin(m_els) + 1  # movie must be positive
    remove_init = 0 # if you need to remove frames from the beginning of each file
    downsample_factor = 1 
    base_name = fname.split('/')[-1][:-4]
    name_new = cm.save_memmap_each(fnames, dview=dview, base_name=base_name, resize_fact=(
        1, 1, downsample_factor), remove_init=remove_init, idx_xy=idx_xy, add_to_movie=add_to_movie, border_to_0=0)
    name_new.sort()

    # If multiple files were saved in C format, put them together in a single large file 
    if len(name_new) > 1:
        fname_new = cm.save_memmap_join(
            name_new, base_name='Yr', n_chunks=20, dview=dview)
    else:
        print('One file only, not saving!')
        fname_new = name_new[0]

    print("Final movie saved in: {}.".format(fname_new))

    Yr, dims, T = cm.load_memmap(fname_new)
    d1, d2 = dims
    images = np.reshape(Yr.T, [T] + list(dims), order='F')
    Y = np.reshape(Yr, dims + (T,), order='F')

    motion_corrected_video_path = os.path.splitext(os.path.basename(video_path))[0] + "_mc.npy"
    np.save(motion_corrected_video_path, Y)

    log_files = glob.glob('Yr*_LOG_*')
    for log_file in log_files:
        os.remove(log_file)

    out = np.zeros(m_els.shape)
    out[:] = m_els[:]

    out = np.nan_to_num(out)

    return out, motion_corrected_video_path
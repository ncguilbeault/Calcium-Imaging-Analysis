from __future__ import division
from param_window import ParamWindow
from preview_window import PreviewWindow
from skimage.morphology import *
import utilities
import time
import json
import os
import sys
import scipy.ndimage as ndi
import scipy.signal
import numpy as np
from skimage.external.tifffile import imread, imsave
from skimage.measure import find_contours, regionprops
from skimage.filters import gaussian
from skimage.restoration import (denoise_tv_chambolle, denoise_bilateral,
                                 denoise_wavelet, estimate_sigma)
from skimage import exposure
import cv2
import matplotlib.pyplot as plt
import csv

# import the Qt library
try:
    from PyQt4.QtCore import *
    from PyQt4.QtGui import *
    pyqt_version = 4
except:
    from PyQt5.QtCore import *
    from PyQt5.QtGui import *
    from PyQt5.QtWidgets import *
    pyqt_version = 5

if sys.version_info[0] < 3:
    python_version = 2
else:
    python_version = 3

class GUIController():
    def __init__(self, controller):
        self.controller = controller
        
        # create windows
        self.param_window   = ParamWindow(self)
        self.preview_window = PreviewWindow(self)

        self.video             = None
        self.image             = None
        self.background_mask   = None
        self.roi_overlay       = None
        self.roi_image         = None
        self.selected_mask     = None
        self.selected_mask_num = -1
        self.rois_erased       = False
        self.figure            = None
        self.figure_closed     = True
        self.selected_roi      = None

        # initialize state variables
        self.closing                      = False
        self.performing_motion_correction = False # whether motion correction is being performed
        self.finding_rois                 = False # whether ROIs are currently being found
        self.processing_videos            = False # whether videos are currently being processed

        # initialize thread variables
        self.motion_correction_thread = None
        self.roi_finding_thread       = None
        self.video_processing_thread  = None

        # set the mode -- "motion_correcting" / "roi_finding" / "roi_filtering"
        self.mode = "motion_correcting"

        # set references to param widgets & preview window
        self.param_widget                   = self.param_window.main_param_widget
        self.motion_correction_param_widget = self.param_window.motion_correction_widget
        self.roi_finding_param_widget       = self.param_window.roi_finding_widget
        self.roi_filtering_param_widget     = self.param_window.roi_filtering_widget

        # set the current z plane to 0
        self.z = 0

    def import_videos(self):
        # let user pick video file(s)
        if pyqt_version == 4:
            video_paths = QFileDialog.getOpenFileNames(self.param_window, 'Select videos to process.', '', 'Videos (*.tif *.tiff)')

            video_paths = [ str(path) for path in video_paths ]
        elif pyqt_version == 5:
            video_paths = QFileDialog.getOpenFileNames(self.param_window, 'Select videos to process.', '', 'Videos (*.tif *.tiff)')[0]

        # import the videos (only the first video is actually opened and previewed, the rest are added to a list of videos to process)
        if video_paths is not None and len(video_paths) > 0:
            self.controller.import_videos(video_paths)

            # set z to 0 if necessary
            if self.z >= self.controller.video.shape[1]:
                self.z = 0

            # notify the param window
            self.param_window.videos_imported(video_paths)

            self.show_video(self.controller.video, self.controller.video_path)

            # reset history variables
            self.reset_history()

            # notify the param window
            self.param_window.video_opened(max_z=self.controller.video.shape[1]-1, z=self.z)

    def show_video(self, video, video_path):
        # calculate gamma- and contrast-adjusted video
        self.video = self.calculate_adjusted_video(video, z=self.z)

        self.preview_window.play_video(self.video, video_path, self.controller.params['fps'])

    def save_mc_video(self):
        # let the user pick where to save the video
        if pyqt_version == 4:
            save_path = str(QFileDialog.getSaveFileName(self.param_window, 'Save Video', '{}_motion_corrected'.format(os.path.splitext(self.controller.video_path)[0]), 'Videos (*.tif *.tiff)'))
        elif pyqt_version == 5:
            save_path = str(QFileDialog.getSaveFileName(self.param_window, 'Save Video', '{}_motion_corrected'.format(os.path.splitext(self.controller.video_path)[0]), 'Videos (*.tif *.tiff)')[0])
        if not (save_path.endswith('.tif') or save_path.endswith('.tiff')):
            save_path += ".tif"

        self.controller.save_mc_video(save_path)

    def save_rois(self):
        # let the user pick where to save the ROIs
        if pyqt_version == 4:
            save_path = str(QFileDialog.getSaveFileName(self.param_window, 'Save ROIs', '{}_rois'.format(os.path.splitext(self.controller.video_path)[0]), 'Numpy (*.npy)'))
        elif pyqt_version == 5:
            save_path = str(QFileDialog.getSaveFileName(self.param_window, 'Save ROIs', '{}_rois'.format(os.path.splitext(self.controller.video_path)[0]), 'Numpy (*.npy)')[0])
        if not save_path.endswith('.npy'):
            save_path += ".npy"

        if save_path is not None and len(save_path) > 0:
            self.controller.save_rois(save_path)

    def load_rois(self):
        # let the user pick saved ROIs
        if pyqt_version == 4:
            load_path = QFileDialog.getOpenFileName(self.param_window, 'Select saved ROI data.', '', 'Numpy (*.npy)')
        elif pyqt_version == 5:
            load_path = QFileDialog.getOpenFileName(self.param_window, 'Select saved ROI data.', '', 'Numpy (*.npy)')[0]

        if load_path is not None and len(load_path) > 0:
            self.controller.load_rois(load_path)

            # stop any motion correction or ROI finding process
            self.cancel_motion_correction()
            self.cancel_roi_finding()

            self.preview_window.timer.stop()

            # reset motion correction progress text
            self.param_widget.update_motion_correction_progress(-1)

            # reset ROI finding progress text
            self.param_widget.update_roi_finding_progress(-1)

            # reset video processing progress text
            self.param_window.update_process_videos_progress(-1)

            # show ROI filtering parameters
            self.show_roi_filtering_params(loading_rois=True)

    def remove_videos_at_indices(self, indices):
        self.controller.remove_videos_at_indices(indices)

        # cancel any ongoing motion correction
        self.cancel_motion_correction()
        self.cancel_processing_videos()
        self.cancel_roi_finding()

        if len(self.controller.video_paths) == 0:
            # switch to showing motion correction params
            self.show_motion_correction_params()

            # reset param window & preview window to their initial states
            self.param_window.set_initial_state()
            self.preview_window.set_initial_state()
        elif 0 in indices:
            # the first video was removed; open the next one for previewing
            self.show_video(self.controller.video, self.controller.video_path)

            # notify the param window
            self.param_window.video_opened(max_z=self.controller.video.shape[1]-1, z=self.z)

    def process_all_videos(self):
        # stop any motion correction or ROI finding process
        self.cancel_motion_correction()
        self.cancel_roi_finding()

        # get rid of filtered out and/or manually erased rois
        if self.controller.removed_rois is not None:
            rois = utilities.remove_rois(self.controller.rois, self.controller.removed_rois)
        else:
            rois = utilities.remove_rois(self.controller.rois, self.controller.filtered_out_rois)

        if not self.processing_videos:
            # cancel any ongoing processing of videos
            self.cancel_processing_videos()

            # create a new thread for processing the videos
            self.video_processing_thread = ProcessVideosThread(self.param_window)
            self.video_processing_thread.connect(self.process_videos_progress)
            self.video_processing_thread.connect(self.process_videos_finished)

            # set its parameters
            self.video_processing_thread.set_parameters(self.controller.video_paths, rois, self.controller.motion_correct_all_videos, self.controller.params["max_shift"], self.controller.params["patch_stride"], self.controller.params["patch_overlap"], self.controller.apply_blur, self.controller.params)

            # start the thread
            self.video_processing_thread.start()

            self.processing_videos = True

            # notify the param window
            self.param_window.process_videos_started()
        else:
            # cancel any ongoing processing of videos
            self.cancel_processing_videos()

    def cancel_processing_videos(self):
        if self.video_processing_thread is not None:
            # inform the thread to stop running
            self.video_processing_thread.running = False

        # reset video processing progress variables
        self.processing_videos       = False
        self.video_processing_thread = None

    def process_videos_progress(self, percent):
        self.param_window.update_process_videos_progress(percent)

    def process_videos_finished(self):
        self.param_window.update_process_videos_progress(100)

        self.processing_videos = False

    def find_rois(self):
        # stop any motion correction or video processing process
        self.cancel_motion_correction()
        self.cancel_processing_videos()

        if not self.finding_rois:
            # cancel any ongoing ROI finding
            self.cancel_roi_finding()

            # create an ROI finding thread
            self.roi_finding_thread = ROIFindingThread(self.roi_finding_param_widget)

            # set the parameters of the ROI finding thread
            self.roi_finding_thread.set_parameters(self.controller.video, self.controller.mean_images, self.controller.masks, self.controller.params["min_area"], self.controller.params["max_area"], self.controller.params["min_circ"], self.controller.params["max_circ"], self.controller.params['soma_threshold'], self.controller.params['window_size'], self.controller.params['background_threshold'], self.controller.params['contrast'], self.controller.params['gamma'], self.controller.video_max, self.controller.params['invert_masks'])

            # start the thread
            self.roi_finding_thread.start()

            self.finding_rois = True

            self.roi_finding_thread.progress.connect(self.roi_finding_progress)
            self.roi_finding_thread.finished.connect(self.roi_finding_ended)

            # notify the param widget
            self.roi_finding_param_widget.roi_finding_started()
        else:
            self.cancel_roi_finding()

    def cancel_roi_finding(self):
        if self.roi_finding_thread is not None:
            # inform the thread to stop running
            self.roi_finding_thread.running = False

        # reset ROI finding progress variables
        self.finding_rois       = False
        self.roi_finding_thread = None

        self.roi_finding_param_widget.update_roi_finding_progress(-1)

    def roi_finding_progress(self, percent):
        # notify the param widget
        self.roi_finding_param_widget.update_roi_finding_progress(percent)

    def roi_finding_ended(self, rois, roi_areas, roi_circs, filtered_out_rois): # TODO: create an roi_finding_ended() method for the param window
        # notify the param widget
        self.roi_finding_param_widget.update_roi_finding_progress(100)

        self.finding_rois = False

        self.controller.rois              = rois
        self.controller.original_rois     = rois[:]
        self.controller.roi_areas         = roi_areas
        self.controller.roi_circs         = roi_circs
        self.controller.filtered_out_rois = filtered_out_rois
        self.controller.removed_rois      = filtered_out_rois[:]

        # update the param window
        self.roi_finding_param_widget.show_rois_checkbox.setDisabled(False)
        self.roi_finding_param_widget.show_rois_checkbox.setChecked(True)
        self.param_window.show_rois_action.setDisabled(False)
        self.param_window.save_roi_image_action.setDisabled(False)
        self.param_window.show_rois_action.setChecked(True)
        self.roi_finding_param_widget.filter_rois_button.setDisabled(False)

        self.controller.mc_rois = self.controller.use_mc_video

        # create ROI image
        rgb_image = cv2.cvtColor(utilities.normalize(self.image, self.controller.video_max), cv2.COLOR_GRAY2RGB)
        self.roi_image, self.roi_overlay = utilities.draw_rois(rgb_image, self.controller.rois[self.z], None, None, self.controller.filtered_out_rois[self.z], None)

        # show the ROI image
        self.show_roi_image(True)

    def motion_correct_video(self):
        # stop any video processing or ROI finding process
        self.cancel_processing_videos()
        self.cancel_roi_finding()

        if not self.performing_motion_correction:
            # determine whether to motion correct all z planes or just the current one (-1 means all z planes)
            if self.controller.mc_current_z:
                mc_z = self.z
            else:
                mc_z = -1

            # cancel any ongoing motion correction
            self.cancel_motion_correction()

            # create a motion correction thread
            self.motion_correction_thread = MotionCorrectThread(self.motion_correction_param_widget)
            self.motion_correction_thread.progress.connect(self.motion_correction_progress)
            self.motion_correction_thread.finished.connect(self.motion_correction_ended)

            # set the parameters of the motion correction thread
            self.motion_correction_thread.set_parameters(self.controller.video, self.controller.video_path, int(self.controller.params["max_shift"]), int(self.controller.params["patch_stride"]), int(self.controller.params["patch_overlap"]), mc_z=mc_z)

            # start the thread
            self.motion_correction_thread.start()

            self.performing_motion_correction = True

            # notify the param widget
            self.motion_correction_param_widget.motion_correction_started()
        else:
            self.cancel_motion_correction()

    def cancel_motion_correction(self):
        if self.motion_correction_thread is not None:
            self.motion_correction_thread.running = False

        self.performing_motion_correction = False
        self.motion_correction_thread     = None

        self.motion_correction_param_widget.update_motion_correction_progress(100)

    def motion_correction_progress(self, percent):
        # notify the param widget
        self.motion_correction_param_widget.update_motion_correction_progress(percent)

    def motion_correction_ended(self, mc_video): # TODO: create a motion_correction_ended() method for the param window
        # notify the param widget
        self.motion_correction_param_widget.update_motion_correction_progress(100)

        self.performing_motion_correction = False

        if np.sum(mc_video) != 0:
            self.controller.mc_video = mc_video

            # calculate the adjusted motion-corrected video at the current z plane
            self.video = self.calculate_adjusted_video(self.controller.mc_video, z=self.z)

            # update the param window
            self.param_window.videos_widget.save_mc_video_button.setEnabled(True)
            self.motion_correction_param_widget.use_mc_video_checkbox.setEnabled(True)
            self.motion_correction_param_widget.use_mc_video_checkbox.setChecked(True)

            self.set_use_mc_video(True)

    def show_motion_correction_params(self):
        # switch to showing motion correction params
        self.param_window.stacked_widget.setCurrentIndex(0)
        self.mode = "motion_correcting"

        self.preview_window.timer.stop()

        # play the video
        if self.controller.use_mc_video and self.controller.mc_video is not None:
            self.show_video(self.controller.mc_video, self.controller.video_path)
        else:
            self.show_video(self.controller.video, self.controller.video_path)

        self.param_window.statusBar().showMessage("")

    def show_roi_finding_params(self):
        # cancel any ongoing motion correction
        self.cancel_motion_correction()

        self.preview_window.timer.stop()

        # switch to showing ROI finding params
        self.param_window.stacked_widget.setCurrentIndex(1)
        self.mode = "roi_finding"

        if self.controller.mc_rois != self.controller.use_mc_video:
            self.controller.find_new_rois = True

        if self.controller.find_new_rois:
            self.controller.reset_roi_finding_variables(reset_rois=True)
            self.controller.reset_roi_filtering_variables(reset_rois=True)

            # calculate mean images
            self.controller.calculate_mean_images()

            # uncheck "Show ROIs" checkbox
            self.roi_finding_param_widget.show_rois_checkbox.setChecked(False)

            self.controller.find_new_rois = False

            # calculate ROI finding variables
            self.image           = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])
            self.background_mask = utilities.calculate_background_mask(self.image, self.controller.params['background_threshold'], self.controller.video_max)
            
        self.show_roi_image(show=self.roi_finding_param_widget.show_rois_checkbox.isChecked())

        self.param_window.statusBar().showMessage("")

        self.preview_window.setWindowTitle(os.path.basename(self.controller.video_path))

    def show_roi_filtering_params(self, loading_rois=False):
        # switch to showing ROI filtering params
        self.param_window.stacked_widget.setCurrentIndex(2)
        self.mode = "roi_filtering"

        if loading_rois:
            # calculate mean images
            self.controller.calculate_mean_images()

            # calculate adjusted image
            self.image = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])

            # reset history variables
            self.reset_history()

        # calculate the ROI image
        self.calculate_roi_image(z=self.z, update_overlay=self.roi_overlay is None)

        # show the ROI image
        self.roi_filtering_param_widget.show_rois_checkbox.setChecked(True)
        self.show_roi_image(self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

        # add the current state to the history
        self.add_to_history()

        self.param_window.statusBar().showMessage("")

    def reset_history(self, z=None):
        # initialize history variables
        if z is None:
            self.last_erased_rois           = [ [] for i in range(self.video.shape[1]) ]
            self.previous_rois              = [ [] for i in range(self.video.shape[1]) ]
            self.previous_roi_overlays      = [ [] for i in range(self.video.shape[1]) ]
            self.previous_erased_rois       = [ [] for i in range(self.video.shape[1]) ]
            self.previous_filtered_out_rois = [ [] for i in range(self.video.shape[1]) ]
            self.previous_adjusted_images   = [ [] for i in range(self.video.shape[1]) ]
            self.previous_roi_images        = [ [] for i in range(self.video.shape[1]) ]
            self.previous_removed_rois      = [ [] for i in range(self.video.shape[1]) ]
            self.previous_locked_rois       = [ [] for i in range(self.video.shape[1]) ]
            self.previous_params            = [ [] for i in range(self.video.shape[1]) ]
        else:
            self.last_erased_rois[z]           = []
            self.previous_rois[z]              = []
            self.previous_roi_overlays[z]      = []
            self.previous_erased_rois[z]       = []
            self.previous_filtered_out_rois[z] = []
            self.previous_adjusted_images[z]   = []
            self.previous_roi_images[z]        = []
            self.previous_removed_rois[z]      = []
            self.previous_locked_rois[z]       = []
            self.previous_params[z]            = []

    def close_all(self):
        # cancel any ongoing threads
        self.cancel_motion_correction()
        self.cancel_roi_finding()
        self.cancel_processing_videos()

        self.closing = True

        # close param & preview windows
        self.param_window.close()
        self.preview_window.close()

        # save the current parameters
        self.save_params()

    def preview_contrast(self, contrast):
        self.controller.params['contrast'] = contrast

        if self.mode == "motion_correcting":
            self.preview_window.timer.stop()

            # calculate a contrast- and gamma-adjusted version of the current frame
            if self.controller.use_mc_video:
                adjusted_frame = self.calculate_adjusted_frame(self.controller.mc_video)
            else:
                adjusted_frame = self.calculate_adjusted_frame(self.controller.video)

            # show the adjusted frame
            self.preview_window.show_frame(adjusted_frame)
        elif self.mode in ("roi_finding", "roi_filtering"):
            self.update_param("contrast", contrast)

    def preview_gamma(self, gamma):
        self.controller.params['gamma'] = gamma

        if self.mode == "motion_correcting":
            self.preview_window.timer.stop()

            # calculate a contrast- and gamma-adjusted version of the current frame
            if self.controller.use_mc_video:
                adjusted_frame = self.calculate_adjusted_frame(self.controller.mc_video)
            else:
                adjusted_frame = self.calculate_adjusted_frame(self.controller.video)

            # show the adjusted frame
            self.preview_window.show_frame(adjusted_frame)
        elif self.mode in ("roi_finding", "roi_filtering"):
            self.update_param("gamma", gamma)

    def update_param(self, param, value):
        # update the parameter
        if param in self.controller.params.keys():
            self.controller.params[param] = value

        if self.mode == "motion_correcting":
            if param in ("contrast, gamma"):
                self.preview_window.timer.stop()

                # play the video
                if self.controller.use_mc_video and self.controller.mc_video is not None:
                    self.show_video(self.controller.mc_video, self.controller.video_path)
                else:
                    self.show_video(self.controller.video, self.controller.video_path)
            elif param == "fps":
                # update the FPS of the preview window
                self.preview_window.set_fps(self.controller.params['fps'])
            elif param == "z":
                self.z = value

                self.preview_window.timer.stop()

                # play the video
                if self.controller.use_mc_video and self.controller.mc_video is not None:
                    self.show_video(self.controller.mc_video, self.controller.video_path)
                else:
                    self.show_video(self.controller.video, self.controller.video_path)
        elif self.mode == "roi_finding":
            if param in ("contrast, gamma"):
                # calculate a contrast- and gamma-adjusted version of the current z plane's mean image
                self.image = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])
                self.background_mask = utilities.calculate_background_mask(self.image, self.controller.params['background_threshold'], self.controller.video_max)

                # update the ROI image using the new adjusted image
                if self.controller.rois is not None:
                    self.calculate_roi_image(self.z, update_overlay=False)

                # show the ROI image
                self.show_roi_image(show=self.roi_finding_param_widget.show_rois_checkbox.isChecked())
            elif param == "background_threshold":
                # calculate the background mask using the new threshold
                self.background_mask = utilities.calculate_background_mask(self.image, self.controller.params['background_threshold'], self.controller.video_max)

                # uncheck the "Show ROIs" checkbox
                self.roi_finding_param_widget.show_rois_checkbox.setChecked(False)
                self.param_window.show_rois_action.setChecked(False)

                # show the background mask
                # TODO: only show the background mask while dragging the slider
                self.preview_window.plot_image(self.image, background_mask=self.background_mask, video_max=self.controller.video_max)
            elif param == "z":
                self.z = value

                # calculate a contrast- and gamma-adjusted version of the new z plane's mean image
                self.image = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])
                self.background_mask = utilities.calculate_background_mask(self.image, self.controller.params['background_threshold'], self.controller.video_max)

                # update the ROI image using the new adjusted image
                if self.controller.rois is not None:
                    self.calculate_roi_image(self.z, update_overlay=True)

                # show the ROI image
                self.show_roi_image(show=self.roi_finding_param_widget.show_rois_checkbox.isChecked())
        elif self.mode == "roi_filtering":
            if param in ("contrast, gamma"):
                # calculate a contrast- and gamma-adjusted version of the current z plane's mean image
                self.image = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])

                # update the ROI image using the new adjusted image
                if self.controller.rois is not None:
                    self.calculate_roi_image(self.z, update_overlay=False)

                # show the ROI image
                self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())
            if param == "z":
                self.z = value

                # calculate a contrast- and gamma-adjusted version of the new z plane's mean image
                self.image = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])

                # filter the ROIs at the new z plane
                self.controller.filter_rois(z=self.z)

                # update the ROI image using the new adjusted image
                self.calculate_roi_image(z=self.z, update_overlay=True)

                # show the ROI image
                self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

                # add the current state to the history only if this is the first time we've switched to this z plane
                self.add_to_history(only_if_new=True)
            elif param in ("min_area", "max_area", "min_circ", "max_circ"):
                pass

    def calculate_adjusted_video(self, video, z=None):
        print(video.shape)

        if z is not None:
            # calculate the adjusted video only at this z plane
            return utilities.adjust_gamma(utilities.adjust_contrast((video[:, z, :, :]), self.controller.params['contrast']), self.controller.params['gamma'])
        else:
            # calculate the adjusted video at all z planes
            return utilities.adjust_gamma(utilities.adjust_contrast((video), self.controller.params['contrast']), self.controller.params['gamma'])

    def calculate_adjusted_frame(self, video):
        # calculate the adjusted frame
        return utilities.adjust_gamma(utilities.adjust_contrast((video[self.preview_window.frame_num, self.z]), self.controller.params['contrast']), self.controller.params['gamma'])

    def set_use_mc_video(self, use_mc_video):
        self.controller.use_mc_video = use_mc_video

        # calculate the corresponding adjusted video and play it
        if self.controller.use_mc_video:
            if self.video is None:
                self.video = self.calculate_adjusted_video(self.controller.mc_video, z=self.z)
        else:
            if self.video is None:
                self.video = self.calculate_adjusted_video(self.controller.video, z=self.z)
        self.preview_window.play_movie(self.video, fps=self.controller.params['fps'])

    def set_mc_current_z(self, mc_current_z):
        self.controller.mc_current_z = mc_current_z

    def set_apply_blur(self, apply_blur):
        self.controller.apply_blur = apply_blur

        # calculate new mean images
        self.controller.calculate_mean_images()

        # calculate a contrast- and gamma-adjusted version of the current z plane's mean image
        self.image = utilities.calculate_adjusted_image(self.controller.mean_images[self.z], self.controller.params['contrast'], self.controller.params['gamma'])
        self.background_mask = utilities.calculate_background_mask(self.image, self.controller.params['background_threshold'], self.controller.video_max)

        # update the ROI image using the new adjusted image
        if self.controller.rois is not None:
            self.calculate_roi_image(self.z, update_overlay=False)


        # show the ROI image
        self.show_roi_image(show=self.roi_finding_param_widget.show_rois_checkbox.isChecked())

    def show_roi_image(self, show):
        # plot the ROI image (or the regular image if show is False)
        if show:
            self.preview_window.plot_image(self.roi_image, background_mask=self.background_mask, video_max=255.0)
        else:
            self.preview_window.plot_image(self.image, background_mask=self.background_mask, video_max=self.controller.video_max)

        # update the param window
        self.param_window.show_rois_action.setChecked(show)
        if self.mode == "roi_finding":
            self.roi_finding_param_widget.show_rois_checkbox.setChecked(show)
        elif self.mode == "roi_filtering":
            self.roi_filtering_param_widget.show_rois_checkbox.setChecked(show)

    def save_roi_image(self):
        # let the user pick where to save the ROI images
        if pyqt_version == 4:
            save_path = str(QFileDialog.getSaveFileName(self.param_window, 'Save ROI image', '{}_rois_z_{}'.format(os.path.splitext(self.controller.video_path)[0], self.z), 'PNG (*.png)'))
        elif pyqt_version == 5:
            save_path = str(QFileDialog.getSaveFileName(self.param_window, 'Save ROI image', '{}_rois_z_{}'.format(os.path.splitext(self.controller.video_path)[0], self.z), 'PNG (*.png)')[0])
        if not save_path.endswith('.png'):
            save_path += ".png"

        if save_path is not None and len(save_path) > 0:
            # save the ROIs image
            scipy.misc.imsave(save_path, self.roi_image)

    def set_invert_masks(self, boolean):
        self.controller.set_invert_masks(boolean)

        self.preview_window.plot_image(self.image, background_mask=self.background_mask, video_max=self.controller.video_max)

    def draw_mask(self):
        if not self.preview_window.drawing_mask:
            self.preview_window.plot_image(self.image, background_mask=self.background_mask, video_max=self.controller.video_max)

            # notify the preview window that we are in mask drawing mode
            self.preview_window.start_drawing_mask()

            self.selected_mask     = None
            self.selected_mask_num = -1

            # update the param widget
            self.roi_finding_param_widget.draw_mask_button.setText("Done")
            self.roi_finding_param_widget.draw_mask_button.previous_message = "Draw a mask on the image preview."
            self.roi_finding_param_widget.param_widget.setEnabled(False)
            self.roi_finding_param_widget.button_widget.setEnabled(False)
            self.roi_finding_param_widget.erase_selected_mask_button.setEnabled(False)
            self.roi_finding_param_widget.draw_mask_button.setEnabled(True)
        else:
            if len(self.preview_window.mask_points) > 0:
                # update the mask points
                mask_points = self.preview_window.mask_points
                mask_points += [mask_points[0]]
                self.controller.mask_points[self.z].append(mask_points)
                mask_points = np.array(mask_points)

                # create the mask
                mask = np.zeros(self.image.shape)
                cv2.fillConvexPoly(mask, mask_points, 1)
                mask = mask.astype(np.bool)

                if self.controller.params['invert_masks']:
                    mask = mask == False

                self.controller.masks[self.z].append(mask)

                self.controller.n_masks += 1

            # notify the preview window that we are no longer in mask drawing mode
            self.preview_window.end_drawing_mask()

            self.preview_window.plot_image(self.image, background_mask=self.background_mask, video_max=self.controller.video_max)

            # update the param widget
            self.roi_finding_param_widget.draw_mask_button.setText("Draw Mask")
            self.roi_finding_param_widget.draw_mask_button.previous_message = ""
            self.roi_finding_param_widget.param_widget.setEnabled(True)
            self.roi_finding_param_widget.button_widget.setEnabled(True)

    def calculate_roi_image(self, z, update_overlay=True, newly_erased_rois=None):
        if update_overlay:
            roi_overlay = None
        else:
            roi_overlay = self.roi_overlay

        # create ROI image
        rgb_image = cv2.cvtColor(utilities.normalize(self.image, self.controller.video_max), cv2.COLOR_GRAY2RGB)
        self.roi_image, self.roi_overlay = utilities.draw_rois(rgb_image, self.controller.rois[z], self.selected_roi, self.controller.erased_rois[z], self.controller.filtered_out_rois[z], self.controller.locked_rois[z], newly_erased_rois=newly_erased_rois, roi_overlay=roi_overlay)

    def select_mask(self, mask_point):
        # figure out which mask is selected (if any)
        selected_mask, selected_mask_num = utilities.get_mask_containing_point(self.controller.masks[self.z], mask_point, inverted=self.controller.params['invert_masks'])

        if selected_mask is not None:
            # update the param widget
            self.roi_finding_param_widget.erase_selected_mask_button.setEnabled(True)

            self.selected_mask     = selected_mask
            self.selected_mask_num = selected_mask_num
        else:
            # update the param widget
            self.roi_finding_param_widget.erase_selected_mask_button.setEnabled(False)

            self.selected_mask     = None
            self.selected_mask_num = -1

        self.show_roi_image(show=self.roi_finding_param_widget.show_rois_checkbox.isChecked())

    def erase_selected_mask(self):
        if self.selected_mask is not None:
            # remove the mask
            del self.controller.masks[self.z][self.selected_mask_num]
            del self.controller.mask_points[self.z][self.selected_mask_num]

            self.selected_mask     = None
            self.selected_mask_num = -1

            # update the param widget
            self.roi_finding_param_widget.erase_selected_mask_button.setEnabled(False)

            self.preview_window.plot_image(self.image, background_mask=self.background_mask, video_max=self.controller.video_max)

    def filter_rois(self, z):
        self.controller.filter_rois(z)

        # update the ROI image
        self.calculate_roi_image(z=self.z, update_overlay=True)

        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

    def draw_rois(self): # TODO: create roi_drawing_started() and roi_drawing_ended() methods for the preview window
        if not self.preview_window.drawing_rois:
            self.preview_window.drawing_rois = True

            # notify the param window
            self.param_window.roi_drawing_started()
        else:
            self.preview_window.drawing_rois = False

            # notify the param window
            self.param_window.roi_drawing_ended()

    def create_roi(self, start_point, end_point):
        # pick a number for this ROI
        label = np.amax(self.controller.rois[self.z])+1

        self.controller.create_roi(start_point, end_point, label, self.z)

        # update the ROI overlay
        utilities.add_roi_to_overlay(self.roi_overlay, self.controller.rois[self.z] == l, self.controller.rois[self.z])

        # calculate the ROI image
        self.calculate_roi_image(z=self.z, update_overlay=False)

        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

        # add this state to the history
        self.add_to_history()

    def shift_rois(self, start_point, end_point):
        self.controller.shift_rois(start_point, end_point, self.z)

        self.roi_overlay  = np.roll(self.roi_overlay, y_shift, axis=0)
        self.roi_overlay  = np.roll(self.roi_overlay, x_shift, axis=1)

        # calculate the ROI image
        self.calculate_roi_image(z=self.z, update_overlay=False)

        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

    def erase_rois(self): # TODO: create roi_erasing_started() and roi_erasing_ended() methods for the preview window
        self.rois_erased = False

        if not self.preview_window.erasing_rois:
            self.selected_roi = None

            self.preview_window.erasing_rois = True

            # notify the param window
            self.param_window.roi_erasing_started()
        else:
            self.preview_window.erasing_rois = False

            # notify the param window
            self.param_window.roi_erasing_ended()

            # add the current state to the history
            self.add_to_history()

    def erase_rois_near_point(self, roi_point, radius=10):
        if not self.rois_erased:
            # create a new list storing the ROIs that are being erased in this erasing operation
            self.last_erased_rois[self.z].append([])
            self.rois_erased = True

        rois_erased = self.controller.erase_rois_near_point(roi_point, self.z, radius=radius)

        if len(rois_erased) > 0:
            # create & show the new ROI image
            self.calculate_roi_image(z=self.z, update_overlay=False, newly_erased_rois=rois_erased)
            self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

    def select_roi(self, roi_point): # TODO: create roi_selected() and roi_unselected() methods for the param window
        if roi_point is not None:
            # find out which ROI to select
            selected_roi = utilities.get_roi_containing_point(self.controller.rois[self.z], roi_point)
        else:
            selected_roi = None

        if selected_roi is not None and selected_roi not in self.controller.removed_rois[self.z]:
            # an ROI is selected

            self.selected_roi = selected_roi

            # create & show the new ROI image
            self.calculate_roi_image(z=self.z, update_overlay=False)
            self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

            # update the param window
            self.roi_filtering_param_widget.lock_roi_button.setEnabled(True)
            self.roi_filtering_param_widget.enlarge_roi_button.setEnabled(True)
            self.roi_filtering_param_widget.shrink_roi_button.setEnabled(True)
            self.roi_filtering_param_widget.erase_selected_roi_button.setEnabled(True)
            if selected_roi in self.controller.locked_rois[self.z]:
                self.roi_filtering_param_widget.lock_roi_button.setText("Unlock ROI")
            else:
                self.roi_filtering_param_widget.lock_roi_button.setText("Lock ROI")

            if self.controller.use_mc_video and self.controller.mc_video is not None:
                video = self.controller.mc_video
            else:
                video = self.controller.video

            # get the activity of this ROI
            activity = utilities.calc_activity_of_roi(self.controller.rois[self.z], video[:, self.z, :, :].transpose(1, 2, 0), self.selected_roi, z=self.z)

            # plot the activity of the ROI
            if self.figure is None:
                plt.close('all')
                self.figure, self.axis = plt.subplots(figsize=(5, 3))
                self.figure.canvas.mpl_connect('close_event', self.figure_closed)
                self.figure.canvas.set_window_title('ROI Activity')
                self.figure.tight_layout()
            self.axis.clear()
            self.axis.plot(activity, c="#FF6666")
            self.figure.canvas.set_window_title('ROI {} Activity'.format(self.selected_roi))
        else:
            # no ROI is selected

            self.selected_roi = -1

            # create & show the new ROI image
            self.calculate_roi_image(z=self.z, update_overlay=False)
            self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

            # update the param window
            self.roi_filtering_param_widget.lock_roi_button.setEnabled(False)
            self.roi_filtering_param_widget.enlarge_roi_button.setEnabled(False)
            self.roi_filtering_param_widget.shrink_roi_button.setEnabled(False)
            self.roi_filtering_param_widget.lock_roi_button.setText("Lock ROI")

    def add_to_history(self, only_if_new=False):
        if (not only_if_new) or len(self.previous_rois[self.z]) == 0:
            print("Adding to history.")

            # only store up to 20 history states
            if len(self.previous_rois[self.z]) > 20:
                del self.previous_rois[self.z][0]
            if len(self.previous_roi_overlays[self.z]) > 20:
                del self.previous_roi_overlays[self.z][0]
            if len(self.previous_erased_rois[self.z]) > 20:
                del self.previous_erased_rois[self.z][0]
            if len(self.previous_filtered_out_rois[self.z]) > 20:
                del self.previous_filtered_out_rois[self.z][0]
            if len(self.previous_adjusted_images[self.z]) > 20:
                del self.previous_adjusted_images[self.z][0]
            if len(self.previous_roi_images[self.z]) > 20:
                del self.previous_roi_images[self.z][0]
            if len(self.previous_removed_rois[self.z]) > 20:
                del self.previous_removed_rois[self.z][0]
            if len(self.previous_locked_rois[self.z]) > 20:
                del self.previous_locked_rois[self.z][0]

            # store the current state
            self.previous_rois[self.z].append(self.controller.rois[self.z][:])
            self.previous_erased_rois[self.z].append(self.controller.erased_rois[self.z][:])
            self.previous_filtered_out_rois[self.z].append(self.controller.filtered_out_rois[self.z][:])
            self.previous_removed_rois[self.z].append(self.controller.removed_rois[self.z][:])
            self.previous_locked_rois[self.z].append(self.controller.locked_rois[self.z][:])

            self.previous_roi_overlays[self.z].append(self.roi_overlay.copy())
            self.previous_adjusted_images[self.z].append(self.image.copy())
            self.previous_roi_images[self.z].append(self.roi_image.copy())

    def undo(self):
        # unselect any ROIs
        self.select_roi(None)

        if len(self.previous_rois[self.z]) > 1:
            del self.previous_rois[self.z][-1]

            self.controller.rois[self.z] = self.previous_rois[self.z][-1][:]
        if len(self.previous_roi_overlays[self.z]) > 1:
            del self.previous_roi_overlays[self.z][-1]

            self.roi_overlay = self.previous_roi_overlays[self.z][-1].copy()
        if len(self.previous_erased_rois[self.z]) > 1:
            del self.previous_erased_rois[self.z][-1]

            self.controller.erased_rois[self.z] = self.previous_erased_rois[self.z][-1][:]
        if len(self.previous_adjusted_images[self.z]) > 1:
            del self.previous_adjusted_images[self.z][-1]

            self.image  = self.previous_adjusted_images[self.z][-1].copy()
        if len(self.previous_roi_images[self.z]) > 1:
            del self.previous_roi_images[self.z][-1]

            self.roi_image = self.previous_roi_images[self.z][-1].copy()
        if len(self.previous_locked_rois[self.z]) > 1:
            del self.previous_locked_rois[self.z][-1]

            self.controller.locked_rois[self.z] = self.previous_locked_rois[self.z][-1][:]

        self.controller.removed_rois[self.z] = self.controller.filtered_out_rois[self.z] + self.controller.erased_rois[self.z]

        # create & show the new ROI image
        self.calculate_roi_image(z=self.z, update_overlay=False)
        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

    def reset_erase(self):
        # unselect any ROIs
        self.select_roi(None)

        if len(self.previous_rois[self.z]) > 0:
            self.controller.rois[self.z] = self.previous_rois[self.z][0][:]
        if len(self.previous_roi_overlays[self.z]) > 0:
            self.roi_overlay = self.previous_roi_overlays[self.z][0].copy()
        if len(self.previous_erased_rois[self.z]) > 0:
            self.controller.erased_rois[self.z] = self.previous_erased_rois[self.z][0][:]
        if len(self.previous_adjusted_images[self.z]) > 0:
            self.image  = self.previous_adjusted_images[self.z][0].copy()
        if len(self.previous_roi_images[self.z]) > 0:
            self.roi_image = self.previous_roi_images[self.z][0].copy()
        if len(self.previous_locked_rois[self.z]) > 0:
            self.controller.locked_rois[self.z] = self.previous_locked_rois[self.z][0][:]

        self.controller.rois[self.z]         = self.original_rois[self.z][:]
        self.controller.removed_rois[self.z] = self.controller.filtered_out_rois[self.z][:]

        # reset the history for this z plane
        self.reset_history(z=self.z)

        # create & show the new ROI image
        self.calculate_roi_image(z=self.z, update_overlay=True)
        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

    def erase_selected_roi(self): # TODO: call roi_unselected() method of the param window
        self.controller.erase_roi(label, z)

        self.selected_roi = None

        # create & show the new ROI image
        self.calculate_roi_image(z=self.z, update_overlay=True)
        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

        # update param widget
        self.roi_filtering_param_widget.erase_selected_roi_button.setEnabled(False)

        # add current state to the history
        self.add_to_history()

    def lock_roi(self): # TODO: create roi_locked() and roi_unlocked() methods for the param window
        if self.selected_roi not in self.controller.locked_rois[self.z]:
            self.controller.locked_rois[self.z].append(self.selected_roi)

            # update the param widget
            self.roi_filtering_param_widget.lock_roi_button.setText("Unlock ROI")
        else:
            index = self.controller.locked_rois[self.z].index(self.selected_roi)
            del self.controller.locked_rois[self.z][index]

            # update the param widget
            self.roi_filtering_param_widget.lock_roi_button.setText("Lock ROI")

        # create & show the new ROI image
        self.calculate_roi_image(z=self.z, update_overlay=False)
        self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

        # add current state to the history
        self.add_to_history()

    def enlarge_roi(self):
        if self.selected_roi >= 1:
            # create a mask of the enlarged ROI
            mask = self.controller.rois[self.z] == self.selected_roi
            mask = binary_dilation(mask, disk(1))

            # draw the new ROI
            self.controller.rois[self.z][mask] = self.selected_roi

            # create & show the new ROI image
            self.calculate_roi_image(z=self.z, update_overlay=True)
            self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

            if self.controller.use_mc_video and self.controller.mc_video is not None:
                video = self.controller.mc_video
            else:
                video = self.controller.video

            # get the activity of this ROI
            activity = utilities.calc_activity_of_roi(self.controller.rois[self.z], video[:, self.z, :, :].transpose(1, 2, 0), self.selected_roi, z=self.z)

            # plot the activity of the ROI
            if self.figure is None:
                plt.close('all')
                self.figure, self.axis = plt.subplots(figsize=(5, 3))
                self.figure.canvas.mpl_connect('close_event', self.figure_closed)
                self.figure.canvas.set_window_title('ROI Activity')
                self.figure.tight_layout()
            self.axis.clear()
            self.axis.plot(activity, c="#FF6666")
            self.figure.canvas.set_window_title('ROI {} Activity'.format(self.selected_roi))

            # add current state to the history
            self.add_to_history()

    def shrink_roi(self):
        if self.selected_roi >= 1:
            # create & draw the shrunken ROI
            rois              = self.controller.rois[self.z].copy()
            mask              = self.controller.rois[self.z] == self.selected_roi
            rois[mask]        = 0
            mask              = erosion(mask, disk(1))
            rois[mask]        = self.selected_roi
            self.controller.rois[self.z] = rois.copy()

            # create & show the new ROI image
            self.calculate_roi_image(z=self.z, update_overlay=True)
            self.show_roi_image(show=self.roi_filtering_param_widget.show_rois_checkbox.isChecked())

            if self.controller.use_mc_video and self.controller.mc_video is not None:
                video = self.controller.mc_video
            else:
                video = self.controller.video

            # get the activity of this ROI
            activity = utilities.calc_activity_of_roi(self.controller.rois[self.z], video[:, self.z, :, :].transpose(1, 2, 0), self.selected_roi, z=self.z)

            # plot the activity of the ROI
            if self.figure is None:
                plt.close('all')
                self.figure, self.axis = plt.subplots(figsize=(5, 3))
                self.figure.canvas.mpl_connect('close_event', self.figure_closed)
                self.figure.canvas.set_window_title('ROI Activity')
                self.figure.tight_layout()
            self.axis.clear()
            self.axis.plot(activity, c="#FF6666")
            self.figure.canvas.set_window_title('ROI {} Activity'.format(self.selected_roi))

            # add current state to the history
            self.add_to_history()

    def erase_selected_rois(self):
        pass

    def merge_selected_rois(self):
        pass

    def figure_closed(self, event):
        self.figure = None

    def save_params(self):
        self.controller.save_params()

    def set_motion_correct(self, boolean):
        self.controller.motion_correct_all_videos = boolean

class MotionCorrectThread(QThread):
    finished = pyqtSignal(np.ndarray)
    progress = pyqtSignal(int)

    def __init__(self, parent):
        QThread.__init__(self, parent)

        self.running = False

    def set_parameters(self, video, video_path, max_shift, patch_stride, patch_overlap, mc_z=-1):
        self.video         = video
        self.video_path    = video_path
        self.max_shift     = max_shift
        self.patch_stride  = patch_stride
        self.patch_overlap = patch_overlap
        self.mc_z          = mc_z

    def run(self):
        self.running = True

        mc_video = utilities.motion_correct(self.video, self.video_path, self.max_shift, self.patch_stride, self.patch_overlap, progress_signal=self.progress, thread=self, mc_z=self.mc_z)

        self.finished.emit(mc_video)

        self.running = False

class ROIFindingThread(QThread):
    finished = pyqtSignal(list, list, list, list)
    progress = pyqtSignal(int)

    def __init__(self, parent):
        QThread.__init__(self, parent)

        self.running = False

    def set_parameters(self, video, mean_images, masks, min_area, max_area, min_circ, max_circ, soma_threshold, window_size, background_threshold, contrast, gamma, video_max, invert_masks):
        self.video                = video
        self.mean_images          = mean_images
        self.masks                = masks
        self.min_area             = min_area
        self.max_area             = max_area
        self.min_circ             = min_circ
        self.max_circ             = max_circ
        self.soma_threshold       = soma_threshold
        self.window_size          = window_size
        self.background_threshold = background_threshold
        self.contrast             = contrast
        self.gamma                = gamma
        self.video_max            = video_max
        self.invert_masks         = invert_masks

        # print(self.min_area, self.max_area, self.min_circ, self.max_circ)

    def run(self):
        rois              = [ [] for i in range(self.video.shape[1]) ]
        roi_areas         = [ [] for i in range(self.video.shape[1]) ]
        roi_circs         = [ [] for i in range(self.video.shape[1]) ]
        filtered_out_rois = [ [] for i in range(self.video.shape[1]) ]

        self.running = True

        for z in range(self.video.shape[1]):
            adjusted_image  = utilities.calculate_adjusted_image(self.mean_images[z], self.contrast, self.gamma)
            background_mask = utilities.calculate_background_mask(adjusted_image, self.background_threshold, self.video_max)
            equalized_image = utilities.calculate_equalized_image(adjusted_image, background_mask, self.window_size, self.video_max)
            soma_mask, I_mod, soma_threshold_image = utilities.calculate_soma_threshold_image(equalized_image, self.soma_threshold, self.video_max)

            if not self.running:
                self.running = False

                return

            self.progress.emit(int(100.0*float(z + (1/3))/self.video.shape[1]))

            if len(self.masks[z]) > 0:
                masks = np.array(self.masks[z])
                if not self.invert_masks:
                    mask = np.sum(masks, axis=0).astype(bool)
                else:
                    mask = np.prod(masks, axis=0).astype(bool)

                soma_mask[mask == False] = 1
                I_mod[mask == False] = 0

            rois[z], roi_areas[z], roi_circs[z] = utilities.find_rois(soma_mask, I_mod, self.mean_images[z])

            if not self.running:
                self.running = False

                return

            self.progress.emit(int(100.0*float(z + (2/3))/self.video.shape[1]))

            if len(self.masks[z]) > 0:
                masks = np.array(self.masks[z])
                mask = np.sum(masks, axis=0).astype(bool)

                out = np.zeros(rois[z].shape).astype(int)
                out[mask] = rois[z][mask]
                rois[z] = out.copy()

            _, filtered_out_rois[z] = utilities.filter_rois(self.mean_images[z], rois[z], self.min_area, self.max_area, self.min_circ, self.max_circ, roi_areas[z], roi_circs[z])

            if not self.running:
                self.running = False

                return

            self.progress.emit(int(100.0*float(z + 1)/self.video.shape[1]))

        if rois is not None:
            self.finished.emit(rois, roi_areas, roi_circs, filtered_out_rois)

        self.running = False

class ProcessVideosThread(QThread):
    finished = pyqtSignal()
    progress = pyqtSignal(int)

    def __init__(self, parent):
        QThread.__init__(self, parent)

        self.running = False

    def set_parameters(self, video_paths, rois, motion_correct, max_shift, patch_stride, patch_overlap, apply_blur, params):
        self.video_paths    = video_paths
        self.rois           = rois
        self.motion_correct = motion_correct
        self.max_shift      = max_shift
        self.patch_stride   = patch_stride
        self.patch_overlap  = patch_overlap
        self.apply_blur     = apply_blur
        self.params         = params

    def run(self):
        self.running = True

        video_shape       = None
        first_mean_images = None
        mean_images       = None

        for i in range(len(self.video_paths)):
            video_path = self.video_paths[i]

            # open video
            base_name = os.path.basename(video_path)
            if base_name.endswith('.npy'):
                video = np.load(video_path)
            elif base_name.endswith('.tif') or base_name.endswith('.tiff'):
                video = imread(video_path)

            print("Processing {}.".format(base_name))

            if len(video.shape) < 3:
                print("Skipping, this file is not a video -- not enough dimensions.")
                continue

            if len(video.shape) == 3:
                # add z dimension
                video = video[:, np.newaxis, :, :]

            if video_shape is None and not self.motion_correct:
                video_shape = video.shape

            video = np.nan_to_num(video).astype(np.float32)

            # figure out the dynamic range of the video
            max_value = np.amax(video)
            if max_value > 2047:
                video_max = 4095
            elif max_value > 1023:
                video_max = 2047
            elif max_value > 511:
                video_max = 1023
            elif max_value > 255:
                video_max = 511
            elif max_value > 1:
                video_max = 255
            else:
                video_max = 1
            
            name           = os.path.splitext(base_name)[0]
            directory      = os.path.dirname(video_path)
            video_dir_path = os.path.join(directory, name)

            # make a folder to hold the results
            if not os.path.exists(video_dir_path):
                os.makedirs(video_dir_path)

            if not self.running:
                self.running = False

                return

            self.progress.emit(int(100.0*float(i + (1/3))/len(self.video_paths)))

            if self.motion_correct:
                print("Performing motion correction...")
                mc_video = utilities.motion_correct(video, video_path, self.max_shift, self.patch_stride, self.patch_overlap)

                if video_shape is None:
                    video_shape = mc_video.shape

                np.save(os.path.join(video_dir_path, '{}_motion_corrected.npy'.format(name)), mc_video)

            if not self.running:
                self.running = False

                return

            self.progress.emit(int(100.0*float(i + (2/3))/len(self.video_paths)))

            rois = self.rois[:]

            print(np.unique(self.rois))

            if self.motion_correct:
                vid = mc_video
            else:
                vid = video

            # print(rois[0].shape, vid.shape, video_shape)

            # if rois[0].shape[0] > vid.shape[2] or rois[0].shape[1] > vid.shape[3]:
            #     print("Cropping rois...")
            #     height_pad =  (rois[0].shape[0] - vid.shape[2])//2
            #     width_pad  =  (rois[0].shape[1] - vid.shape[3])//2

            #     for i in range(len(rois)):
            #         rois[i] = rois[i][height_pad:, width_pad:]
            #         rois[i] = rois[i][:vid.shape[2], :vid.shape[3]]
            # elif rois[0].shape[0] < vid.shape[2] or rois[0].shape[1] < vid.shape[3]:
            #     print("Padding rois...")
            #     height_pad_pre =  (vid.shape[2] - rois[0].shape[0])//2
            #     width_pad_pre  =  (vid.shape[3] - rois[0].shape[1])//2

            #     height_pad_post = vid.shape[2] - rois[0].shape[0] - height_pad_pre
            #     width_pad_post  = vid.shape[3] - rois[0].shape[1] - width_pad_pre

            #     # print(height_pad_pre, height_pad_post, width_pad_pre, width_pad_post)

            #     for i in range(len(rois)):
            #         rois[i] = np.pad(rois[i], ((height_pad_pre, height_pad_post), (width_pad_pre, width_pad_post)), 'constant')

            # print(rois[0].shape, vid.shape, video_shape)

            # shift the rois to match the first video
            if self.apply_blur:
                mean_images = [ ndi.median_filter(utilities.sharpen(ndi.gaussian_filter(denoise_tv_chambolle(utilities.mean(vid, z), weight=0.01, multichannel=False), 1)), 3) for z in range(vid.shape[1]) ]
            else:
                mean_images = [ utilities.mean(vid, z) for z in range(vid.shape[1]) ]

            for z in range(vid.shape[1]):
                if first_mean_images is not None:
                    y_shift, x_shift = utilities.calculate_shift(first_mean_images[z], mean_images[z])

                    if np.abs(y_shift) < 20 and np.abs(x_shift) < 20:
                        rois[z] = np.roll(rois[z], -y_shift, axis=0)
                        rois[z] = np.roll(rois[z], -x_shift, axis=1)

                        if y_shift >= 0 and x_shift >= 0:
                            rois[z][:y_shift, :] = 0
                            rois[z][:, :x_shift] = 0
                        elif y_shift < 0 and x_shift >= 0:
                            rois[z][y_shift:, :] = 0
                            rois[z][:, :x_shift] = 0
                        elif y_shift >= 0 and x_shift < 0:
                            rois[z][:y_shift, :] = 0
                            rois[z][:, x_shift:] = 0
                        else:
                            rois[z][y_shift:, :] = 0
                            rois[z][:, x_shift:] = 0

                # print("Saving ROI image of z plane {}...".format(z))

                # adjusted_image = utilities.calculate_adjusted_image(mean_images[z], self.params['contrast'], self.params['gamma'])

                # rgb_image = cv2.cvtColor(utilities.normalize(adjusted_image, video_max), cv2.COLOR_GRAY2RGB)

                # roi_image, _ = utilities.draw_rois(rgb_image, rois[z], None, None, [], None, roi_overlay=None)

                # cv2.imwrite(os.path.join(video_dir_path, 'z_{}_rois.png'.format(z)), roi_image)

            np.save(os.path.join(video_dir_path, 'all_rois.npy'), rois)

            if first_mean_images is None:
                first_mean_images = mean_images[:]

            results = [ {} for z in range(video.shape[1]) ]

            for z in range(video.shape[1]):
                np.save(os.path.join(video_dir_path, 'z_{}_rois.npy'.format(z)), rois[z])

                print("Calculating ROI activities for z={}...".format(z))
                centroids, traces = utilities.calculate_centroids_and_traces(rois[z], vid[:, z, :, :])

                print("Saving CSV for z={}...".format(z))

                roi_nums = np.unique(rois[z]).tolist()
                # remove ROI #0 (this is the background)
                try:
                    index = roi_nums.index(0)
                    del roi_nums[index]
                except:
                    pass

                with open(os.path.join(video_dir_path, 'z_{}_traces.csv'.format(z)), 'w') as file:
                    writer = csv.writer(file)

                    writer.writerow([''] + [ "ROI #{}".format(roi) for roi in roi_nums ])

                    for i in range(traces.shape[0]):
                        writer.writerow([i+1] + traces[i].tolist())

                with open(os.path.join(video_dir_path, 'z_{}_centroids.csv'.format(z)), 'w') as file:
                    writer = csv.writer(file)

                    writer.writerow(['Label', 'X', 'Y'])

                    for i in range(centroids.shape[0]):
                        writer.writerow(["ROI #{}".format(roi_nums[i])] + centroids[i].tolist())

                print("Done.")

                # vid_z = vid[:, z, :, :].transpose(1, 2, 0)
                # roi_nums = np.unique(rois[z])
                # last_percent_printed = 0
                # for i in range(len(roi_nums)):
                #     l = roi_nums[i]
                #     percent = int((i/len(roi_nums))*100.0)
                #     if percent % 10 == 0 and percent > last_percent_printed:
                #         print("{}% done.".format(percent))
                #         last_percent_printed = percent
                #     activity = utilities.calc_activity_of_roi(rois[z], vid_z, l, z=z)

                #     results[z][l] = activity


                # print("Saving CSV for z={}...".format(z))
                # with open(os.path.join(video_dir_path, 'z_{}_traces.csv'.format(z)), 'w') as file:
                #     writer = csv.writer(file)

                #     writer.writerow(['ROI #'] + [ 'Frame {}'.format(i) for i in range(video.shape[0]) ])

                #     for l in np.unique(self.rois[z])[1:]:
                #         writer.writerow([l] + results[z][l].tolist())
                # print("Done.")

            if not self.running:
                self.running = False

                return

            self.progress.emit(int(100.0*float(i + 1)/len(self.video_paths)))

            np.savez(os.path.join(video_dir_path, '{}_roi_traces.npz'.format(os.path.splitext(video_path)[0])), results)

        self.finished.emit()

        self.running = False
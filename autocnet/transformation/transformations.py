import abc
import math
from collections import deque
import warnings

import cv2
import numpy as np
import pandas as pd
import pysal as ps
from scipy import optimize

from autocnet.utils.utils import make_homogeneous, normalize_vector, crossform
from autocnet.camera import camera
from autocnet.camera import utils as camera_utils


class TransformationMatrix(np.ndarray):
    """
    Abstract Base Class representing a 3x3 transformation matrix.
    This ABC subclasses numpy ndarrays.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def __new__(cls, ndarray, index):

        obj = np.asarray(ndarray).view(cls)
        obj.index = index
        obj.mask = pd.Series(True, index=index)
        obj._action_stack = deque(maxlen=10)
        obj._current_action_stack = 0
        obj._observers = set()

        state_package = {'arr': obj.copy(),
                         'mask': None}
        obj._action_stack.append(state_package)

        return obj

    @abc.abstractmethod
    def __array_finalize__(self, obj):
        if obj is None:
            return
        self._action_stack = getattr(obj, '_action_stack', None)
        self._current_action_stack = getattr(obj, '_current_action_stack', None)
        self._observers = getattr(obj, '_observers', None)

        self.x1 = getattr(obj, 'x1', None)
        self.x2 = getattr(obj, 'x2', None)
        self.index = getattr(obj, 'index', None)
        self.mask = getattr(obj, 'mask', None)

    @abc.abstractproperty
    def determinant(self):
        return np.linalg.det(self)

    @abc.abstractproperty
    def rank(self):
        return np.linalg.matrix_rank(self)

    @abc.abstractproperty
    def condition(self):
        """
        The condition is a measure of the numerical stability of the
        solution to a set of linear equations.
        """
        if not getattr(self, '_condition', None):
            s = np.linalg.svd(self, compute_uv=False)
            self._condition = s[0] / s[1]
        return self._condition

    @abc.abstractproperty
    def error(self):
        if not hasattr(self, '_error'):
            self._error = self.compute_error(self.x1,
                                             self.x2,
                                             self.mask)
        return self._error

    @abc.abstractproperty
    def describe_error(self):
        return self.error.describe()

    @abc.abstractmethod
    def rollback(self, n=1):
        """
        Roll backward in the object histroy, e.g. undo

        Parameters
        ----------

        n : int
            the number of steps to roll backwards
        """
        idx = self._current_action_stack - n
        if idx < 0:
            idx = 0
        self._current_action_stack = idx
        state = self._action_stack[idx]
        self[:] = state['arr']
        setattr(self, 'mask', state['mask'])
        # Reset attributes (could also cache)
        self._clean_attrs()
        self._notify_subscribers(self)

    @abc.abstractmethod
    def rollforward(self, n=1):
        """
        Roll forwards in the object history, e.g. do

        Parameters
        ----------

        n : int
            the number of steps to roll forwards
        """
        idx = self._current_action_stack + n
        if idx > len(self._action_stack) - 1:
            idx = len(self._action_stack) - 1
        self._current_action_stack = idx
        state = self._action_stack[idx]
        self[:] = state['arr']
        setattr(self, 'mask', state['mask'])
        # Reset attributes (could also cache)
        self._clean_attrs()
        self._notify_subscribers(self)

    @abc.abstractmethod
    def subscribe(self, func):
        """
        Subscribe some observer to the edge

        Parameters
        ----------

        func : object
               The callable that is to be executed on update
        """
        self._observers.add(func)

    @abc.abstractmethod
    def _notify_subscribers(self, *args, **kwargs):
        """
        The 'update' call to notify all subscribers of
        a change.
        """
        for update_func in self._observers:
            update_func(self, *args, **kwargs)

    @abc.abstractmethod
    def compute_error(self, x1, x2, index=None):
        raise NotImplementedError

    @abc.abstractmethod
    def recompute_matrix(self):
        raise NotImplementedError

    @abc.abstractmethod
    def refine(self):
        raise NotImplementedError


class FundamentalMatrix(TransformationMatrix):
    """
    A homography or planar transformation matrix

    Attributes
    ----------

    x1 : ndarray
         (n,2) array of point correspondences used to compute F

    x2 : ndarray
         (n,2) array of point correspondences used to compute F

    mask : ndarray
           (n, 2) boolean array indicating whether a correspondence is
           considered an inliner

    determinant : float
                  The determinant of the matrix

    condition : float
                The condition computed as SVD[0] / SVD[-1]

    error : dataframe
            describing the error of the points used to
            compute this homography
    """

    def refine_matches(self, threshold=1.0):
        """
        Given a Fundamental matrix, recheck all matches and update the
        mask given a threshold.

        Parameters
        ----------
        threshold : float
                    The new upper, reprojective error limit, in pixels.
        """
        state_package = {'arr': self[:].copy(),
                         'mask': self.mask.copy()}

        error = self.compute_error(self.x1, self.x2)
        self.mask = error <= threshold

        self._action_stack.append(state_package)
        self._current_action_stack = len(self._action_stack) - 1  # 0 based vs. 1 based
        self._clean_attrs()
        self._notify_subscribers(self)

    def _clean_attrs(self):
        for a in ['_error', '_determinant', '_condition']:
            try:
                delattr(self, a)
            except:
                pass

    @property
    def error(self):
        """
        Using the currently unmasked correspondences, compute the reprojection
        error.

        Returns
        -------
        : ndarray
          The current error

        See Also
        --------
        compute_error : The method called to compute element-wise error.
        """

        x = self.x1[self.mask]
        x1 = self.x2[self.mask]
        return self.compute_error(x, x1)

    def compute_error(self, x, x1):
        """
        Given a set of matches and a known fundamental matrix,
        compute distance between all match points and the associated
        epipolar lines.

        Ideal error is defined by $x^{\intercal}Fx = 0$, where x
        where $x$ are all matchpoints in a given image and
        $x^{\intercal}F$ defines the standard form of the
        epipolar line in the second image.

        The distance between a point and the associated epipolar
        line is computed as: $d = \frac{\lvert ax_{0} + by_{0} + c \rvert}{\sqrt{a^{2} + b^{2}}}$.

        Parameters
        ----------

        x : dataframe
            n,3 dataframe of homogeneous coordinates

        x1 : dataframe
            n,3 dataframe of homogeneous coordinates with the same
            length as argument x

        Returns
        -------
        F_error : ndarray
                  n,1 vector of reprojection errors
        """

        # Normalize the vector
        l_norms = normalize_vector(x.dot(self.T))
        F_error = np.abs(np.sum(l_norms * x1, axis=1))

        return F_error

    def compute(self, kp1, kp2, method='mle', reproj_threshold=2.0, confidence=0.99):
        """
        Given two arrays of keypoints compute the fundamental matrix

        Parameters
        ----------
        kp1 : arraylike
              (n, 2) of coordinates from the source image

        kp2 : ndarray
              (n, 2) of coordinates from the destination image


        method : {'ransac', 'lmeds', 'normal', '8point'}
                  The openCV algorithm to use for outlier detection

        reproj_threshold : float
                           The maximum distances in pixels a reprojected points
                           can be from the epipolar line to be considered an inlier

        confidence : float
                     [0, 1] that the estimated matrix is correct

        Notes
        -----
        While the method is user definable, if the number of input points
        is < 7, normal outlier detection is automatically used, if 7 > n > 15,
        least medians is used, and if 7 > 15, ransac can be used.
        """
        if method == 'mle':
            # Grab an initial estimate using RANSAC, then apply MLE
            method_ = cv2.FM_RANSAC
        elif method == 'ransac':
            method_ = cv2.FM_RANSAC
        elif method == 'lmeds':
            method_ = cv2.FM_LMEDS
        elif method == 'normal':
            method_ = cv2.FM_7POINT
        elif method == '8point':
            method_ = cv2.FM_8POINT
        else:
            raise ValueError("Unknown estimation method. Choices are: 'lme', 'ransac', 'lmeds', '8point', or 'normal'.")

        # OpenCV wants arrays
        F, mask = cv2.findFundamentalMat(np.asarray(kp1),
                                         np.asarray(kp2),
                                         method_,
                                         param1=reproj_threshold,
                                         param2=confidence)

        try:
            mask = mask.astype(bool).ravel()  # Enforce dimensionality
        except:
            return  # pragma: no cover

        # Ensure that the singularity constraint is met
        self._enforce_singularity_constraint()

        # Set instance variables to inputs
        self.x1 = pd.DataFrame(kp1, index=self.index)
        self.x2 = pd.DataFrame(kp2, index=self.index)
        self.mask = pd.Series(mask, index=self.index)

        try:
            self[:] = F
        except:
            warnings.warn('F computation fell back to 7-point algorithm and returned 3 F matrices.')
            return

        if method == 'mle':
            # Now apply the gold standard algorithm to refine F

            # Generate an idealized and to be updated camera model
            p1 = camera.estimated_camera_from_f(F)
            p = camera.idealized_camera()

            # Grab the points used to estimate F
            pt = self.x1.loc[self.mask].T
            pt1 = self.x2.loc[self.mask].T

            if pt.shape[1] < 9 or pt1.shape[1] < 9:
                warnings.warn("Unable to apply MLE.  Not enough correspondences.")
                return

            # Apply Levenber-Marquardt to perform a non-linear lst. squares fit
            #  to minimize triangulation error (this is a local bundle)
            result = optimize.least_squares(camera.projection_error, p1.ravel(),
                                            args=(p, pt, pt1),
                                            method='lm')

            gold_standard_p = result.x.reshape(3, 4) # SciPy Lst. Sq. requires a vector, camera is 3x4
            optimality = result.optimality
            gold_standard_f = camera_utils.crossform(gold_standard_p[:,3]).dot(gold_standard_p[:,:3])

            self[:] = gold_standard_f


    def _enforce_singularity_constraint(self):
        """
        The fundamental matrix should be rank 2.  In instances when it is not,
        the singularity constraint should be enforced.  This is forces epipolar lines
        to be conincident.

        References
        ----------
        .. [Hartley2003]

        """
        if self.rank != 2:
            u, d, vt = np.linalg.svd(self)
            f1 = u.dot(np.diag([d[0], d[1], 0])).dot(vt)
            self[:] = f1

    def recompute_matrix(self):
        raise NotImplementedError


class Homography(TransformationMatrix):
    """
    A homography or planar transformation matrix

    Attributes
    ----------

    determinant : float
                  The determinant of the matrix

    condition : float
                The condition computed as SVD[0] / SVD[-1]

    error : dataframe
            describing the error of the points used to
            compute this homography
    """

    @property
    def error(self):
        return self.compute_error(self.x1, self.x2)

    @property
    def inverse(self):
        return np.linalg.inv(self)

    def compute_error(self, a, b, mask=None):
        """
        Give this homography, compute the planar reprojection error
        between points a and b.

        Parameters
        ----------

        a : ndarray
            n,2 array of x,y coordinates

        b : ndarray
            n,2 array of x,y coordinates

        index : ndarray
                Index to be used in the returned dataframe

        Returns
        -------

        df : dataframe
             With columns for x_residual, y_residual, rmse, and
             error contribution.  The dataframe also has cumulative
             x, t, and total RMS statistics accessible via
             df.x_rms, df.y_rms, and df.total_rms, respectively.
        """

        if mask is not None:
            mask = mask
        else:
            mask = pd.Series(True, index=self.index)

        a = a[mask].values
        b = b[mask].values

        if a.shape[1] == 2:
            a = make_homogeneous(a)
        if b.shape[1] == 2:
            b = make_homogeneous(b)

        # ToDo: Vectorize for performance
        for i, j in enumerate(a):
            a[i] = self.dot(j)
            a[i] /= a[i][-1]

        data = np.empty((a.shape[0], 4))

        data[:, 0] = x_res = b[:, 0] - a[:, 0]
        data[:, 1] = y_res = b[:, 1] - a[:, 1]
        data[:, 2] = rms = np.sqrt(x_res**2 + y_res**2)
        total_rms = np.sqrt(np.mean(x_res**2 + y_res**2))
        x_rms = np.sqrt(np.mean(x_res**2))
        y_rms = np.sqrt(np.mean(y_res**2))

        data[:, 3] = rms / total_rms

        df = pd.DataFrame(data,
                          columns=['x_residuals',
                                   'y_residuals',
                                   'rmse',
                                   'error_contribution'],
                          index=self.index)

        df.total_rms = total_rms
        df.x_rms = x_rms
        df.y_rms = y_rms
        return df

    def compute(self, kp1, kp2, method='ransac', reproj_threshold=2.0):
        """
        Compute a planar homography given two sets of keypoints


        Parameters
        ----------
        kp1 : ndarray
              (n, 2) of coordinates from the source image

        kp2 : ndarray
              (n, 2) of coordinates from the destination image

        method : {'ransac', 'lmeds', 'normal'}
                 The openCV algorithm to use for outlier detection

        reproj_threshold : float
                           The maximum distances in pixels a reprojected points
                           can be from the epipolar line to be considered an inlier
        """
        self.x1 = kp1
        self.x2 = kp2

        if method == 'ransac':
            method_ = cv2.RANSAC
        elif method == 'lmeds':
            method_ = cv2.LMEDS
        elif method == 'normal':
            method_ = 0  # Normal method
        else:
            raise ValueError("Unknown outlier detection method.  Choices are: 'ransac', 'lmeds', or 'normal'.")
        transformation_matrix, mask = cv2.findHomography(kp1,
                                                         kp2,
                                                         method_,
                                                         reproj_threshold)
        if mask is not None:
            mask = mask.astype(bool)
        self.mask = mask
        self[:] = transformation_matrix

    def recompute_matrix(self):
        raise NotImplementedError

    def refine(self):
        raise NotImplementedError

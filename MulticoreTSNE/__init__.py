from __future__ import print_function
from glob import glob
import threading
import os
import sys

import numpy as np
import cffi

'''
    Helper class to execute TSNE in separate thread.
'''


class FuncThread(threading.Thread):
    def __init__(self, target, *args):
        threading.Thread.__init__(self)
        self._target = target
        self._args = args

    def run(self):
        self._target(*self._args)


class MulticoreTSNE:
    """
    Compute t-SNE embedding using Barnes-Hut optimizatirandom_stateon and
    multiple cores (if available).

    Parameters mostly correspond to parameters of `sklearn.manifold.TSNE`.

    The following parameters are unused:
    * n_iter_without_progress
    * min_grad_norm
    * method

    Args:
    disjoint_set_size (default=0): if > 0 than dataset is splited on 2 parts: X[:disjoint_set_size] and X[disjoint_set_size:].
                                   Distances between points from different sets are ignored in the loss function.
                                   Can be used only with metric="precomputed".
    contrib_cost_pairs (default=0):

    metric: which metric to use to build VPTree
        - euclidean
        - sqequclidean: usually leads to the same quality as euqclidean, but much faster.
        - cosine
        - angular
        -precomputed
    should_normalize_input: if true normalize input features to zero mean
        and rescale values in each column to have max_value=1. Will be ignored if metric='precomputed'.
    lr_mult: None or np.array of double.
        Defines the learning rate multiplier for each of the embedding points during optimization.
        If None - lr_mu;t=1.0 for every point.

    Parameter `init` doesn't support 'pca' initialization, but a precomputed
    array can be passed.
    """
    def __init__(self,
                 n_components=2,
                 perplexity=30.0,
                 disjoint_set_size=0,
                 early_exaggeration=12,
                 learning_rate=200,
                 n_iter=1000,
                 n_iter_without_progress=30,
                 min_grad_norm=1e-07,
                 metric='euclidean',
                 init='random',
                 lr_mult=None,
                 verbose=0,
                 random_state=None,
                 method='barnes_hut',
                 angle=0.5,
                 should_normalize_input=True,
                 contrib_cost_pairs=0,
                 n_jobs=1):
        self.n_components = n_components
        self.angle = angle
        self.should_normalize_input = should_normalize_input
        self.contrib_cost_pairs = contrib_cost_pairs
        self.perplexity = perplexity
        self.disjoint_set_size = disjoint_set_size
        self.early_exaggeration = early_exaggeration
        self.learning_rate = learning_rate
        self.n_iter = n_iter
        self.n_jobs = n_jobs
        self.random_state = -1 if random_state is None else random_state
        self.metric = metric
        self.init = init
        self.lr_mult = lr_mult
        self.embedding_ = None
        self.n_iter_ = None
        self.kl_divergence_ = None
        self.pairs_error = None
        self.verbose = int(verbose)
        if early_exaggeration <= 0:
            raise ValueError('early_exaggeration must be > 0')
        if disjoint_set_size < 0:
            raise ValueError('disjoint_set_size must be >= 0')

        assert method == 'barnes_hut', 'Only Barnes-Hut method is allowed'
        if disjoint_set_size > 0 and metric != 'precomputed':
            raise ValueError('Disjoint sets are allowed only for metric="precomputed"')

        assert isinstance(init, np.ndarray) or init == 'random', "init must be 'random' or array"
        if isinstance(init, np.ndarray):
            assert init.ndim == 2, "init array must be 2D"
            assert init.shape[1] == n_components, "init array must be of shape (n_instances, n_components)"
            self.init = np.ascontiguousarray(init, float)
            if isinstance(self.lr_mult, np.ndarray) and self.lr_mult.dtype == 'double':
                pass
            elif self.lr_mult is not None:
                raise ValueError(' when init != "random" lr_mult must be None or a double np.array')
        elif init != 'random':
            raise ValueError('init must be "random" or np.array')
        elif self.lr_mult is not None:
            raise ValueError('lr_mult must be None if init = "random". '
                             'There is no sense in changing learning rate multipliers for random points.')

        self.ffi = cffi.FFI()
        self.ffi.cdef(
            """void tsne_run_double(double* X, int N, int D, double* Y,
                                    int no_dims, double perplexity, double theta,
                                    int disjoint_set_size,
                                    int num_threads, int max_iter, int random_state,
                                    bool init_from_Y, double* lr_mult, int verbose,
                                    double early_exaggeration, double learning_rate,
                                    double *final_error, 
                                    double *final_pairs_error,
                                    char* metric,
                                    bool should_normalize_input,
                                    double contrib_cost_pairs,
                                    int* pairs,
                                    int n_pairs);""")

        path = os.path.dirname(os.path.realpath(__file__))
        try:
            sofile = (glob(os.path.join(path, 'libtsne*.so')) +
                      glob(os.path.join(path, '*tsne*.dll')))[0]
            self.C = self.ffi.dlopen(os.path.join(path, sofile))
        except (IndexError, OSError) as e:
            print(e)
            raise RuntimeError('Cannot find/open tsne_multicore shared library')

    def fit(self, X, y=None, pairs=None):
        self.fit_transform(X, y, pairs)
        return self

    def fit_transform(self, X, _y=None, pairs=None):
        """

        Args:
            X:
            _y:
            pairs: list of pairs or [M x 2] array of indices of points that must be close
                in the embedding space

        Returns:

        """
        if self.lr_mult is not None and len(self.lr_mult) != len(X):
            raise ValueError('lr_mult must be a double np.array '
                             'with the same number of elements as in X, '
                             '{} != {}'.format(len(self.lr_mult), len(X)))

        assert X.ndim == 2, 'X should be 2D array.'

        number_nns_to_consider = int(3 * self.perplexity)
        if number_nns_to_consider > len(X) - 1:
            raise ValueError('too large perplexity ({}) for size of the dataset {}'.format(self.perplexity, len(X)))
        if self.disjoint_set_size and \
                (number_nns_to_consider > self.disjoint_set_size - 1 or (number_nns_to_consider > len(X) - self.disjoint_set_size - 1)):
            raise ValueError('too large perplexity({}) for disjoint_set_size({}, {})'.format(self.perplexity,
                                                                                             self.disjoint_set_size,
                                                                                             len(X) - self.disjoint_set_size))


        if self.metric.endswith('_prenormed') and not self.metric.endswith('time_prenormed'):
            norms = (X**2).sum(axis=1)
            if not np.allclose(norms, 1.0):
                raise ValueError('each row of X must have norm 1')

        # X may be modified, make a copy
        X = np.array(X, dtype=float, order='C', copy=True)

        N, D = X.shape
        init_from_Y = isinstance(self.init, np.ndarray)
        if init_from_Y:
            Y = self.init.copy('C')
            assert X.shape[0] == Y.shape[0], "n_instances in init array and X must match"
        else:
            Y = np.zeros((N, self.n_components))

        cffi_X = self.ffi.cast('double*', X.ctypes.data)
        cffi_Y = self.ffi.cast('double*', Y.ctypes.data)
        final_error = np.array(0, dtype=float)
        final_pairs_error = np.array(0, dtype=float)
        cffi_final_error = self.ffi.cast('double*', final_error.ctypes.data)
        cffi_final_pairs_error = self.ffi.cast('double*', final_pairs_error.ctypes.data)

        cffi_metric = self.ffi.new('char[]', self.metric.encode('ascii'))

        if self.lr_mult is not None:
            lr_mult = self.lr_mult.copy('C')
            cffi_lr_mult = self.ffi.cast('double*', lr_mult.ctypes.data)
        else:
            cffi_lr_mult = self.ffi.cast('double*', 0)


        if pairs is not None:
            pairs = np.array(pairs, dtype=np.int32, order='C', copy=True)
            cffi_pairs = self.ffi.cast('int*', pairs.ctypes.data)
            n_pairs = len(pairs)
        else:
            cffi_pairs = self.ffi.cast('int*', 0)
            n_pairs = 0

        t = FuncThread(self.C.tsne_run_double,
                       cffi_X, N, D,
                       cffi_Y, self.n_components,
                       self.perplexity, self.angle,
                       self.disjoint_set_size,
                       self.n_jobs, self.n_iter, self.random_state,
                       init_from_Y, cffi_lr_mult, self.verbose, self.early_exaggeration,
                       self.learning_rate,
                       cffi_final_error, cffi_final_pairs_error,
                       cffi_metric, self.should_normalize_input,
                       self.contrib_cost_pairs, cffi_pairs, n_pairs)
        t.daemon = True
        t.start()

        while t.is_alive():
            t.join(timeout=1.0)
            sys.stdout.flush()

        self.embedding_ = Y
        self.kl_divergence_ = final_error
        self.pairs_error = final_pairs_error
        self.n_iter_ = self.n_iter

        return Y

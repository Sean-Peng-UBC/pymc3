import numpy as np
import theano
from theano import tensor as tt

import pymc3 as pm
from pymc3.distributions.dist_math import rho2sd, log_normal
from pymc3.variational.opvi import GroupApprox, node_property
from pymc3.util import update_start_vals
from pymc3.theanof import batched_diag
from pymc3.variational import flows


__all__ = [
    'MeanField',
    'FullRank',
    'Empirical',
    'NormalizingFlow',
    'sample_approx'
]


class MeanField(GroupApprox):
    R"""Mean Field approximation to the posterior where spherical Gaussian family
    is fitted to minimize KL divergence from True posterior. It is assumed
    that latent space variables are uncorrelated that is the main drawback
    of the method
    """

    @node_property
    def mean(self):
        return self.shared_params['mu']

    @node_property
    def rho(self):
        return self.shared_params['rho']

    @node_property
    def cov(self):
        var = rho2sd(self.rho)**2
        if self.is_local:
            return batched_diag(var)
        else:
            return tt.diag(var)

    @node_property
    def std(self):
        return rho2sd(self.rho)

    def __init_group__(self, group):
        super(MeanField, self).__init_group__(group)
        if not self.user_params:
            self.shared_params = self.create_shared_params(
                self._kwargs.get('start', None)
            )
        self._finalize_init()

    def create_shared_params(self, start=None):
        if start is None:
            start = self.model.test_point
        else:
            start_ = self.model.test_point.copy()
            update_start_vals(start_, start, self.model)
            start = start_
        start = self.bij.map(start)
        return {'mu': theano.shared(
                    pm.floatX(start), 'mu'),
                'rho': theano.shared(
                    pm.floatX(np.zeros((self.ndim,))), 'rho')}

    @node_property
    def symbolic_random(self):
        initial = self.symbolic_initial
        sd = rho2sd(self.rho)
        mu = self.mean
        return sd * initial + mu

    @node_property
    def symbolic_logq(self):
        """
        log_q_W samples over q for global vars
        """
        z = self.symbolic_random
        logq = log_normal(z, self.mean, rho=self.rho)
        return logq.sum(range(1, logq.ndim))


class FullRank(GroupApprox):
    """Full Rank approximation to the posterior where Multivariate Gaussian family
    is fitted to minimize KL divergence from True posterior. In contrast to
    MeanField approach correlations between variables are taken in account. The
    main drawback of the method is computational cost.

    References
    ----------
    -   Geoffrey Roeder, Yuhuai Wu, David Duvenaud, 2016
        Sticking the Landing: A Simple Reduced-Variance Gradient for ADVI
        approximateinference.org/accepted/RoederEtAl2016.pdf
    """
    def __init_group__(self, group):
        super(FullRank, self).__init_group__(group)
        if not self.user_params:
            self.shared_params = self.create_shared_params(
                self._kwargs.get('start', None)
            )
        self._finalize_init()

    def create_shared_params(self, start=None):
        if start is None:
            start = self.model.test_point
        else:
            start_ = self.model.test_point.copy()
            update_start_vals(start_, start, self.model)
            start = start_
        start = pm.floatX(self.bij.map(start))
        n = self.ndim
        L_tril = (
            np.eye(n)
            [np.tril_indices(n)]
            .astype(theano.config.floatX)
        )
        return {'mu': theano.shared(start, 'mu'),
                'L_tril': theano.shared(L_tril, 'L_tril')}

    @node_property
    def L(self):
        return self.shared_params['L_tril'][..., self.tril_index_matrix]

    @node_property
    def mean(self):
        return self.shared_params['mu']

    @node_property
    def cov(self):
        L = self.L
        if self.is_local:
            return tt.batched_dot(L, L.swapaxes(-1, -2))
        else:
            return L.dot(L.T)

    @property
    def num_tril_entries(self):
        n = self.ndim
        return int(n * (n + 1) / 2)

    @property
    def tril_index_matrix(self):
        n = self.ndim
        num_tril_entries = self.num_tril_entries
        tril_index_matrix = np.zeros([n, n], dtype=int)
        tril_index_matrix[np.tril_indices(n)] = np.arange(num_tril_entries)
        tril_index_matrix[
            np.tril_indices(n)[::-1]
        ] = np.arange(num_tril_entries)
        return tril_index_matrix

    @node_property
    def symbolic_logq(self):
        z = self.symbolic_random
        if self.is_local:
            def logq(z_b, mu_b, L_b):
                return pm.MvNormal.dist(mu=mu_b, chol=L_b).logp(z_b)
            # it's gonna be so slow
            # scan is computed over batch and then summed up
            # output shape is (batch, samples)
            return theano.scan(logq, [z.swapaxes(0, 1), self.mean, self.L])[0].sum(0)
        else:
            return pm.MvNormal.dist(mu=self.mean, chol=self.L).logp(z)

    @node_property
    def symbolic_random(self):
        initial = self.symbolic_initial.swapaxes(0, 1)
        L = self.L
        mu = self.mean
        if self.is_local:
            return tt.batched_dot(L, initial).T + mu
        else:
            return L.dot(initial).T + mu

    @classmethod
    def from_mean_field(cls, mean_field):
        """Construct FullRank from MeanField approximation

        Parameters
        ----------
        mean_field : :class:`MeanField`
            approximation to start with

        Returns
        -------
        :class:`FullRank`
        """
        if mean_field.is_local:
            raise TypeError('Cannot init from local group')
        full_rank = object.__new__(cls)  # type: FullRank
        full_rank.__dict__.update(mean_field.__dict__)
        full_rank.shared_params = full_rank.create_shared_params()
        full_rank.shared_params['mu'].set_value(
            mean_field.shared_params['mu'].get_value()
        )
        rho = mean_field.shared_params['rho'].get_value()
        n = full_rank.ndim
        L_tril = (
            np.diag(np.log1p(np.exp(rho)))  # rho2sd
            [np.tril_indices(n)]
            .astype(theano.config.floatX)
        )
        full_rank.shared_params['L_tril'].set_value(L_tril)
        return full_rank


class Empirical(GroupApprox):
    """Builds Approximation instance from a given trace,
    it has the same interface as variational approximation
    Examples
    --------
    >>> with model:
    ...     step = NUTS()
    ...     trace = sample(1000, step=step)
    ...     histogram = Empirical(trace[100:])
    """

    def __init_group__(self, group):
        super(Empirical, self).__init_group__(group)
        self._check_trace()
        if not self.user_params:
            self.shared_params = self.create_shared_params(
                trace=self._kwargs.get('trace', None),
                size=self._kwargs.get('size', None),
                jitter=self._kwargs.get('size', 1),
                start=self._kwargs.get('start', 1)
            )
        self._finalize_init()

    def create_shared_params(self, trace=None, size=None, jitter=1, start=None):
        if trace is None:
            if size is None:
                raise ValueError('Need `trace` or `size` to initialize')
            else:
                if start is None:
                    start = self.model.test_point
                else:
                    start_ = self.model.test_point.copy()
                    update_start_vals(start_, start, self.model)
                    start = start_
                start = pm.floatX(self.bij.map(start))
                # Initialize particles
                histogram = np.tile(start, (size, 1))
                histogram += pm.floatX(np.random.normal(0, jitter, histogram.shape))

        else:
            histogram = np.empty((len(trace) * len(trace.chains), self.ndim))
            i = 0
            for t in trace.chains:
                for j in range(len(trace)):
                    histogram[i] = self.bij.map(trace.point(j, t))
                    i += 1
        return dict(histogram=theano.shared(pm.floatX(histogram), 'histogram'))

    def _check_trace(self):
        trace = self._kwargs.get('trace', None)
        if (trace is not None
            and not all([var.name in trace.varnames
                         for var in self.group])):
            raise ValueError('trace has not all FreeRV in the group')

    def randidx(self, size=None):
        if size is None:
            size = (1,)
        elif isinstance(size, tt.TensorVariable):
            if size.ndim < 1:
                size = size[None]
            elif size.ndim > 1:
                raise ValueError('size ndim should be no more than 1d')
            else:
                pass
        else:
            size = tuple(np.atleast_1d(size))
        return (self._rng
                .uniform(size=size,
                         low=pm.floatX(0),
                         high=pm.floatX(self.histogram.shape[0]) - pm.floatX(1e-16))
                .astype('int32'))

    def _new_initial(self, size, deterministic):
        theano_condition_is_here = isinstance(deterministic, tt.Variable)
        if theano_condition_is_here:
            return tt.switch(
                deterministic,
                tt.repeat(
                    self.mean.dimshuffle('x', 0),
                    size if size is not None else 1, -1),
                self.histogram[self.randidx(size)])
        else:
            if deterministic:
                return tt.repeat(
                    self.mean.dimshuffle('x', 0),
                    size if size is not None else 1, -1)
            else:
                return self.histogram[self.randidx(size)]

    @property
    def symbolic_random(self):
        return self.symbolic_initial

    @property
    def histogram(self):
        """Shortcut to flattened Trace
        """
        return self.shared_params['histogram']

    @node_property
    def mean(self):
        return self.histogram.mean(0)

    @node_property
    def cov(self):
        x = (self.histogram - self.mean)
        return x.T.dot(x) / pm.floatX(self.histogram.shape[0])

    @classmethod
    def from_noise(cls, size, jitter=.01, start=None, **kwargs):
        """Initialize Histogram with random noise

        Parameters
        ----------
        size : `int`
            number of initial particles
        jitter : `float`
            initial sd
        start : `Point`
            initial point
        kwargs : other kwargs passed to init

        Returns
        -------
        :class:`Empirical`
        """
        if 'trace' in kwargs:
            raise ValueError('Trace cannot be passed via kwargs in this constructor')
        return cls(
            trace=None,
            size=size,
            jitter=jitter,
            start=start,
            **kwargs)


class NormalizingFlow(GroupApprox):
    R"""
    Normalizing flow is a series of invertible transformations on initial distribution.

    .. math::

        z_K = f_K \circ \dots \circ f_2 \circ f_1(z_0)

    In that case we can compute tractable density for the flow.

    .. math::

        \ln q_K(z_K) = \ln q_0(z_0) - \sum_{k=1}^{K}\ln \left|\frac{\partial f_k}{\partial z_{k-1}}\right|


    Every :math:`f_k` here is a parametric function with defined determinant.
    We can choose every step here. For example the here is a simple flow
    is an affine transform:

    .. math::

        z = loc(scale(z_0)) = \mu + \sigma * z_0

    Here we get mean field approximation if :math:`z_0 \sim \mathcal{N}(0, 1)`

    **Flow Formulas**

    In PyMC3 there is a flexible way to define flows with formulas. We have 5 of them by the moment:

    -   Loc (:code:`loc`): :math:`z' = z + \mu`
    -   Scale (:code:`scale`): :math:`z' = \sigma * z`
    -   Planar (:code:`planar`): :math:`z' = z + u * \tanh(w^T z + b)`
    -   Radial (:code:`radial`): :math:`z' = z + \beta (\alpha + (z-z_r))^{-1}(z-z_r)`
    -   Householder (:code:`hh`): :math:`z' = H z`

    Formula can be written as a string, e.g. `'scale-loc'`, `'scale-hh*4-loc'`, `'panar*10'`.
    Every step is separated with `'-'`, repeated flow is marked with `'*'` produsing `'flow*repeats'`.

    Parameters
    ----------
    flow : str|AbstractFlow
        formula or initialized Flow, default is `'scale-loc'` that
        is identical to MeanField
    local_rv : dict[var->tuple]
        Experimental for Empirical Approximation
        mapping {model_variable -> local_variable (:math:`\mu`, :math:`\rho`)}
        Local Vars are used for Autoencoding Variational Bayes
        See (AEVB; Kingma and Welling, 2014) for details
    scale_cost_to_minibatch : `bool`
        Scale cost to minibatch instead of full dataset, default False
    model : :class:`pymc3.Model`
        PyMC3 model for inference
    random_seed : None or int
        leave None to use package global RandomStream or other
        valid value to create instance specific one
    jitter : float
        noise for flows' parameters initialization

    References
    ----------
    -   Danilo Jimenez Rezende, Shakir Mohamed, 2015
        Variational Inference with Normalizing Flows
        arXiv:1505.05770

    -   Jakub M. Tomczak, Max Welling, 2016
        Improving Variational Auto-Encoders using Householder Flow
        arXiv:1611.09630
    """

    def __init_group__(self, group):
        flow = self._kwargs.get('flow', 'scale-loc')
        jitter = self._kwargs.get('jitter', 1)
        if isinstance(flow, str):
            flow = flows.Formula(flow)(
                dim=self.ndim,
                z0=self.symbolic_initial,
                jitter=jitter
            )
        self.flow = flow

    @property
    def shared_params(self):
        params = dict()
        current = self.flow
        i = 0
        params[i] = current.shared_params
        while not current.isroot:
            i += 1
            current = current.parent
            params[i] = current.shared_params
        return params

    @shared_params.setter
    def shared_params(self, value):
        current = self.flow
        i = 0
        current.shared_params = value[i]
        while not current.isroot:
            i += 1
            current = current.parent
            current.shared_params = value[i]

    @property
    def params(self):
        return self.flow.all_params

    @node_property
    def symbolic_log_q_W_global(self):
        z0 = self.symbolic_initial_global_matrix
        q0 = pm.Normal.dist().logp(z0).sum(-1)
        return q0-self.gflow.sum_logdets

    @property
    def symbolic_random_global_matrix(self):
        return self.gflow.forward


def sample_approx(approx, draws=100, include_transformed=True):
    """Draw samples from variational posterior.

    Parameters
    ----------
    approx : :class:`Approximation`
        Approximation to sample from
    draws : `int`
        Number of random samples.
    include_transformed : `bool`
        If True, transformed variables are also sampled. Default is True.

    Returns
    -------
    trace : class:`pymc3.backends.base.MultiTrace`
        Samples drawn from variational posterior.
    """
    return approx.sample(draws=draws, include_transformed=include_transformed)

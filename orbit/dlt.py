import pandas as pd
import numpy as np
from scipy.stats import nct
import torch
from copy import deepcopy

from orbit.lgt import LGT
from orbit.constants import dlt
from orbit.exceptions import (
    PredictionException,
    IllegalArgument
)
from orbit.utils.utils import is_ordered_datetime


class DLT(LGT):
    """Implementation of Damped-Local-Trend (LGT) model with seasonality.


    Prediction

    LGT follows state space decomposition such that predictions are sum of the three states--
    trend, seasonality and externality (regression). In math, we have

    .. math::
        \hat{y}_t=\mu_t+s_t+r_t

    Update Process

    Except externality, states are updated in a sequential manner.  In math, we have

    .. math::
        r_t=\{X\\beta\}_t

        \mu_t=l_{t-1}+\\theta_{loc}{b_{t-1}}+\\theta_{glb}{|l_{t-1}|^{\lambda}}

        l_t=\\alpha_{lev}(y_t-s_t-r_t)+(1-\\alpha_{lev})l_{t-1}

        b_t=\\alpha_{slp}(l_t-l_{t-1})+(1-\\alpha_{slp})b_{t-1}

        s_t=\\alpha_{sea}(y_t-l_t-r_t)+(1-\\alpha_{sea})s_{t-1}


    Likelihood & Prior

    .. math::
        y_t\sim{Stt(\hat{y},v,\sigma_{obs})}

        \\beta\sim{N(0,\sigma_{\\beta})}

        \sigma_{obs}\sim Cauchy(0, C_{scale})


    Parameters
    ----------
    regressor_col : list
        a list of regressor column names in the dataframe
    regressor_sign : list
        a list of either {'+', '='} to determine if regression estimates should be restricted
        to positive weights only. The length of `regressor_sign` must be the same as
        `regressor_col`
    regressor_beta_prior : list
        prior values for regressor betas. The length of `regressor_beta_prior` must be the
        same as `regressor_col`
    regressor_sigma_prior : list
        prior values for regressors standard deviation. The length of `regressor_sigma_prior` must
        be the same as `regressor_col`
    is_multiplicative : bool
        if True, response and regressor values are log transformed such that the model is
        multiplicative. If False, no transformations are applied. Default True.
    auto_scale : bool
        **EXPERIMENTAL AND UNSTABLE** if True, response and regressor values are transformed
        with a `MinMaxScaler` such that the min value is `e` and max value is
        the max value in the data
    cauchy_sd : float
        scale parameter of prior of observation residuals scale parameter `C_scale`
    seasonality : int
        The length of the seasonality period. For example, if dataframe contains weekly data
        points, `52` would indicate a yearly seasonality.
    seasonality_min : float
        minimum value allowed for initial seasonality samples
    seasonality_max : float
        maximum value allowed for initial seasonality samples
    seasonality_smoothing_min : float
        minimum value allowed for seasonality smoothing coefficient samples
    seasonality_smoothing_max : float
        maximum value allowed for seasonality smoothing coefficient samples
    global_trend_coef_min : float
        minimum value allowed for global trend coefficient samples
    global_trend_coef_max : float
        maximum value allowed for global trend coefficient samples
    global_trend_pow_min : float
        minimum value allowed for global trend power samples
    global_trend_pow_max : float
        maximum value allowed for global trend power samples
    local_trend_coef_min : float
        minimum value allowed for local trend coefficient samples
    local_trend_coef_max : float
        maximum value allowed for local trend coefficient samples
    level_smoothing_min : float
         minimum value allowed for level smoothing coefficient samples
    level_smoothing_max : float
        maximum value allowed for level smoothing coefficient samples
    slope_smoothing_min : float
        minimum value allowed for local slope smoothing coefficient samples
    slope_smoothing_max : float
        maximum value allowed for local slope smoothing coefficient samples
    fix_regression_coef_sd : int
        binary input 0 for using point prior of regressors sigma; 1 for using Cauchy prior for regressor
        sigma
    regressor_sigma_sd : float
        scale parameter of prior of regressor coefficient scale parameter.
        Ignored when `fix_regression_coef_sd` is 1.
    damped_factor_fixed : float
        input between 0 and 1 which specify damped effect of local slope per period.
    damped_factor_min : float
         minimum value allowed for damped factor samples. Ignored when `damped_factor_fixed` > 0
    damped_factor_max : float
         maximum value allowed for damped factor  samples. Ignored when `damped_factor_fixed` > 0
    regression_coef_max : float
        Maximum absolute value allowed for regression coefficient samples


    Notes
    -----
    LGT model is an extensions of traditional exponential smoothing models. For details, see
    https://otexts.com/fpp2/holt-winters.html.
    In short, the model follows state space decomposition such that predictions are the
    sum of the three states--trend, seasonality and externality (regression).  The states updates
    sequentially with smoothing, scale and other parameters.

    The original model was created by Slawek Smyl, Christoph Bergmeir, Erwin Wibowo,
    To Wang(Edwin) Ng. For details, see https://cran.r-project.org/web/packages/Rlgt/index.html

    We re-parameterized the model to reduce complexity and to be in additive form so that user has
    an option to transform the model back to multiplicative form through logarithm
    transformation.  These changes enhance  speed and stability of sampling. The model also provides
    more support on external regressors.

    One may note that there is a small but critical difference of the formula in both training (*.stan file) and
    prediction (predict()) such that the `l(t)` is updated with levels only l(t-1) rather than
    the integrated trend (like l(t-1) + b(t-1) in traditional exp. smoothing models).
    """
    # this must be defined in child class
    _stan_input_mapper = dlt.StanInputMapper

    def __init__(
            self, regressor_col=None, regressor_sign=None,
            regressor_beta_prior=None, regressor_sigma_prior=None,
            is_multiplicative=True, auto_scale=False, cauchy_sd=None, min_nu=5, max_nu=40,
            seasonality=0, seasonality_min=-1.0, seasonality_max=1.0,
            seasonality_smoothing_min=0, seasonality_smoothing_max=1,
            level_smoothing_min=0, level_smoothing_max=1,
            slope_smoothing_min=0, slope_smoothing_max=1,
            damped_factor_min=0.8, damped_factor_max=0.999,
            use_log_global_trend=True,
            regression_coef_max=1.0, fix_regression_coef_sd=1, regressor_sigma_sd=1.0,
            damped_factor_fixed=0.8, **kwargs
    ):

        # get all init args and values and set
        local_params = {k: v for (k, v) in locals().items() if k not in ['kwargs', 'self']}
        kw_params = locals()['kwargs']

        self.set_params(**local_params)
        super(LGT, self).__init__(**kwargs)  # note this is the base class

        # associates with the *.stan model resource
        self.stan_model_name = "dlt"
        # self.pyro_model_name = "orbit.pyro.dlt.DLTModel--WIP"

    def _set_model_param_names(self):
        self.model_param_names = []
        self.model_param_names += [param.value for param in dlt.BaseStanSamplingParameters]

        # append seasonality param names
        if self.seasonality > 1:
            self.model_param_names += [param.value for param in dlt.SeasonalityStanSamplingParameters]

        # append damped trend param names
        if self.damped_factor_fixed < 0:
            self.model_param_names += [
                param.value for param in dlt.DampedTrendStanSamplingParameters]

        # append log global trend param names
        # if self.use_log_global_trend:
        #     self.model_param_names += [
        #         param.value for param in dlt.LogGlobalTrendSamplingParameters]

        # append positive regressors if any
        if self.num_of_positive_regressors > 0:
            self.model_param_names += [
                dlt.RegressionStanSamplingParameters.POSITIVE_REGRESSOR_BETA.value]

        # append regular regressors if any
        if self.num_of_regular_regressors > 0:
            self.model_param_names += [
                dlt.RegressionStanSamplingParameters.REGULAR_REGRESSOR_BETA.value]

    def _predict(self, df=None, include_error=False, decompose=False):

        ################################################################
        # Model Attributes
        ################################################################

        # get model attributes
        model = deepcopy(self._posterior_state)

        for k, v in model.items():
            model[k] = torch.from_numpy(v)

        # We can pull any arbitrary value from teh dictionary because we hold the
        # safe assumption: the length of the first dimension is always the number of samples
        # thus can be safely used to determine `num_sample`. If predict_method is anything
        # other than full, the value here should be 1
        arbitrary_posterior_value = list(model.values())[0]
        num_sample = arbitrary_posterior_value.shape[0]

        # seasonality components
        seasonality_levels = model.get(
            dlt.SeasonalityStanSamplingParameters.SEASONALITY_LEVELS.value)
        seasonality_smoothing_factor = model.get(
            dlt.SeasonalityStanSamplingParameters.SEASONALITY_SMOOTHING_FACTOR.value
        )

        # trend components
        slope_smoothing_factor = model.get(
            dlt.BaseStanSamplingParameters.SLOPE_SMOOTHING_FACTOR.value)
        level_smoothing_factor = model.get(
            dlt.BaseStanSamplingParameters.LEVEL_SMOOTHING_FACTOR.value)
        local_trend_levels = model.get(dlt.BaseStanSamplingParameters.LOCAL_TREND_LEVELS.value)
        local_trend_slopes = model.get(dlt.BaseStanSamplingParameters.LOCAL_TREND_SLOPES.value)
        local_trend = model.get(dlt.BaseStanSamplingParameters.LOCAL_TREND.value)
        residual_degree_of_freedom = model.get(
            dlt.BaseStanSamplingParameters.RESIDUAL_DEGREE_OF_FREEDOM.value)
        residual_sigma = model.get(dlt.BaseStanSamplingParameters.RESIDUAL_SIGMA.value)

        # set an additional attribute for damped factor when it is fixed
        # get it through user input field
        if self.damped_factor_fixed > 0:
            damped_factor = torch.empty(num_sample, dtype=torch.double)
            damped_factor.fill_(self.damped_factor_fixed)
        else:
            damped_factor = model.get(dlt.DampedTrendStanSamplingParameters.DAMPED_FACTOR.value)

        global_trend_level = model.get(dlt.BaseStanSamplingParameters.GLOBAL_TREND_LEVEL.value)
        global_trend_slope = model.get(dlt.BaseStanSamplingParameters.GLOBAL_TREND_SLOPE.value)
        # if self.use_log_global_trend:
        #     global_trend_shape = model.get(
        #         dlt.LogGlobalTrendSamplingParameters.GLOBAL_TREND_SHAPE.value)
        global_trend = model.get(dlt.BaseStanSamplingParameters.GLOBAL_TREND.value)

        # regression components
        pr_beta = model.get(dlt.RegressionStanSamplingParameters.POSITIVE_REGRESSOR_BETA.value)
        rr_beta = model.get(dlt.RegressionStanSamplingParameters.REGULAR_REGRESSOR_BETA.value)
        if pr_beta is not None and rr_beta is not None:
            pr_beta = pr_beta if len(pr_beta.shape) == 2 else pr_beta.reshape(1, -1)
            rr_beta = rr_beta if len(rr_beta.shape) == 2 else rr_beta.reshape(1, -1)
            regressor_beta = torch.cat((pr_beta, rr_beta), dim=1)
        else:
            regressor_beta = pr_beta or rr_beta

        ################################################################
        # Prediction Attributes
        ################################################################

        # get training df meta
        training_df_meta = self.training_df_meta
        # remove reference from original input
        df = df.copy()
        if self.auto_scale:
            df = self._scale_df(df, do_fit=False)
        # for multiplicative model
        if self.is_multiplicative:
            df = self._log_transform_df(df, do_fit=False)

        # get prediction df meta
        prediction_df_meta = {
            'date_array': pd.to_datetime(df[self.date_col]).reset_index(drop=True),
            'df_length': len(df.index),
            'prediction_start': df[self.date_col].iloc[0],
            'prediction_end': df[self.date_col].iloc[-1]
        }

        if not is_ordered_datetime(prediction_df_meta['date_array']):
            raise IllegalArgument('Datetime index must be ordered and not repeat')

        # TODO: validate that all regressor columns are present, if any

        if prediction_df_meta['prediction_start'] < training_df_meta['training_start']:
            raise PredictionException('Prediction start must be after training start.')

        trained_len = training_df_meta['df_length']
        output_len = prediction_df_meta['df_length']

        # If we cannot find a match of prediction range, assume prediction starts right after train
        # end
        if prediction_df_meta['prediction_start'] > training_df_meta['training_end']:
            forecast_dates = set(prediction_df_meta['date_array'])
            n_forecast_steps = len(forecast_dates)
            # time index for prediction start
            start = trained_len
        else:
            # compute how many steps to forecast
            forecast_dates = \
                set(prediction_df_meta['date_array']) - set(training_df_meta['date_array'])
            # check if prediction df is a subset of training df
            # e.g. "negative" forecast steps
            n_forecast_steps = len(forecast_dates) or \
                               -(len(set(training_df_meta['date_array']) - set(
                                   prediction_df_meta['date_array'])))
            # time index for prediction start
            start = pd.Index(
                training_df_meta['date_array']).get_loc(prediction_df_meta['prediction_start'])

        full_len = trained_len + n_forecast_steps

        ################################################################
        # Regression Component
        ################################################################

        # calculate regression component
        if self.regressor_col is not None and len(self.regular_regressor_col) > 0:
            regressor_beta = regressor_beta.t()
            regressor_matrix = df[self.regressor_col].values
            regressor_torch = torch.from_numpy(regressor_matrix)
            regressor_component = torch.matmul(regressor_torch, regressor_beta)
            regressor_component = regressor_component.t()
        else:
            # regressor is always dependent with df. hence, no need to make full size
            regressor_component = torch.zeros((num_sample, output_len), dtype=torch.double)

        ################################################################
        # Seasonality Component
        ################################################################

        # calculate seasonality component
        if self.seasonality > 1:
            if full_len <= seasonality_levels.shape[1]:
                seasonality_component = seasonality_levels[:, :full_len]
            else:
                seasonality_forecast_length = full_len - seasonality_levels.shape[1]
                seasonality_forecast_matrix = \
                    torch.zeros((num_sample, seasonality_forecast_length), dtype=torch.double)
                seasonality_component = torch.cat(
                    (seasonality_levels, seasonality_forecast_matrix), dim=1)
        else:
            seasonality_component = torch.zeros((num_sample, full_len), dtype=torch.double)

        ################################################################
        # Trend Component
        ################################################################

        # calculate level component.
        # However, if predicted end of period > training period, update with out-of-samples forecast
        if full_len <= trained_len:
            full_local_trend = local_trend[:, :full_len]
            full_global_trend = global_trend[:, :full_len]
        else:
            trend_forecast_length = full_len - trained_len
            trend_forecast_init = \
                torch.zeros((num_sample, trend_forecast_length), dtype=torch.double)
            full_local_trend = torch.cat((local_trend[:, :full_len], trend_forecast_init), dim=1)
            full_global_trend = torch.cat((global_trend[:, :full_len], trend_forecast_init), dim=1)
            last_local_trend_level = local_trend_levels[:, -1]
            last_local_trend_slope = local_trend_slopes[:, -1]

            for idx in range(trained_len, full_len):
                # based on model, split cases for trend update
                curr_local_trend = \
                    last_local_trend_level + damped_factor.flatten() * last_local_trend_slope
                full_local_trend[:, idx] = curr_local_trend
                # full_global_trend[:, idx] = full_global_trend[:, idx - 1] + global_trend_slope
                # if self.use_log_global_trend:
                #     full_global_trend[:, idx] = \
                #         global_trend_level + global_trend_slope * torch.log(
                #             1 + global_trend_shape * (idx - 1))
                if self.use_log_global_trend:
                    full_global_trend[:, idx] = \
                        global_trend_level + torch.log(1 + global_trend_slope * idx)
                else:
                    full_global_trend[:, idx] = \
                        global_trend_level + global_trend_slope * (idx - 1)

                if include_error:
                    error_value = nct.rvs(
                        df=residual_degree_of_freedom,
                        nc=0,
                        loc=0,
                        scale=residual_sigma,
                        size=num_sample
                    )
                    error_value = torch.from_numpy(error_value).double()
                    # for convenience, we lump error on local trend since the formula would
                    # yield the same as yhat + noise - global_trend - seasonality - regression
                    # equivalent with local_trend + noise
                    full_local_trend[:, idx] += error_value

                # now full_local_trend contains the error term and hence we need to use
                # curr_local_trend as a proxy of previous level index
                new_local_trend_level = \
                    level_smoothing_factor * full_local_trend[:, idx] \
                    + (1 - level_smoothing_factor) * curr_local_trend
                last_local_trend_slope = \
                    slope_smoothing_factor * (new_local_trend_level - last_local_trend_level) \
                    + (1 - slope_smoothing_factor) * damped_factor.flatten() * last_local_trend_slope

                if self.seasonality > 1 and idx + self.seasonality < full_len:
                    seasonality_component[:, idx + self.seasonality] = \
                        seasonality_smoothing_factor.flatten() \
                        * (full_local_trend[:, idx] + seasonality_component[:, idx] -
                           new_local_trend_level) \
                        + (1 - seasonality_smoothing_factor.flatten()) * seasonality_component[:,
                                                                         idx]

                last_local_trend_level = new_local_trend_level

        ################################################################
        # Combine Components
        ################################################################

        # trim component with right start index
        trend_component = full_global_trend[:, start:] + full_local_trend[:, start:]
        seasonality_component = seasonality_component[:, start:]

        # sum components
        pred_array = trend_component + seasonality_component + regressor_component

        # for the multiplicative case
        if self.is_multiplicative:
            pred_array = (torch.exp(pred_array)).numpy()
            trend_component = (torch.exp(trend_component)).numpy()
            seasonality_component = (torch.exp(seasonality_component)).numpy()
            regressor_component = (torch.exp(regressor_component)).numpy()
        else:
            pred_array = pred_array.numpy()
            trend_component = trend_component.numpy()
            seasonality_component = seasonality_component.numpy()
            regressor_component = regressor_component.numpy()

        if self.auto_scale:
            # work around response_min_max_scaler initial shape
            init_shape = pred_array.shape
            # enfroce a 2D array
            pred_array = np.reshape(pred_array, (-1, 1))
            pred_array = self.response_min_max_scaler.inverse_transform(pred_array)
            pred_array = pred_array.reshape(init_shape)
            # we assume the unit is based on trend component while others are multipliers
            trend_component = self.response_min_max_scaler.inverse_transform(trend_component)

        # if decompose output dictionary of components
        if decompose:
            decomp_dict = {
                'prediction': pred_array,
                'trend': trend_component,
                'seasonality': seasonality_component,
                'regression': regressor_component
            }

            return decomp_dict

        return {'prediction': pred_array}

    def _validate_params(self):
        pass

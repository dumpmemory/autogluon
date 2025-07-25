from __future__ import annotations

import copy
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd
from typing_extensions import Self

from autogluon.common import space
from autogluon.common.loaders import load_pkl
from autogluon.common.savers import save_pkl
from autogluon.common.utils.resource_utils import get_resource_manager
from autogluon.common.utils.utils import setup_outputdir
from autogluon.core.constants import AG_ARGS_FIT, REFIT_FULL_SUFFIX
from autogluon.core.models import ModelBase
from autogluon.core.utils.exceptions import TimeLimitExceeded
from autogluon.timeseries.dataset import TimeSeriesDataFrame
from autogluon.timeseries.metrics import TimeSeriesScorer, check_get_evaluation_metric
from autogluon.timeseries.regressor import CovariateRegressor, get_covariate_regressor
from autogluon.timeseries.transforms import CovariateScaler, TargetScaler, get_covariate_scaler, get_target_scaler
from autogluon.timeseries.utils.features import CovariateMetadata
from autogluon.timeseries.utils.forecast import make_future_data_frame

from .tunable import TimeSeriesTunable

logger = logging.getLogger(__name__)


class TimeSeriesModelBase(ModelBase, ABC):
    """Abstract base class for all `Model` objects in autogluon.timeseries, including both
    forecasting models and forecast combination/ensemble models.

    Parameters
    ----------
    path : str, default = None
        Directory location to store all outputs.
        If None, a new unique time-stamped directory is chosen.
    freq: str
        Frequency string (cf. gluonts frequency strings) describing the frequency
        of the time series data. For example, "h" for hourly or "D" for daily data.
    prediction_length: int
        Length of the prediction horizon, i.e., the number of time steps the model
        is fit to forecast.
    name : str, default = None
        Name of the subdirectory inside path where model will be saved.
        The final model directory will be os.path.join(path, name)
        If None, defaults to the model's class name: self.__class__.__name__
    covariate_metadata: CovariateMetadata
        A mapping of different covariate types known to autogluon.timeseries to column names
        in the data set.
    eval_metric : Union[str, TimeSeriesScorer], default = "WQL"
        Metric by which predictions will be ultimately evaluated on future test data. This only impacts
        ``model.score()``, as eval_metric is not used during training. Available metrics can be found in
        ``autogluon.timeseries.metrics``.
    hyperparameters : dict, default = None
        Hyperparameters that will be used by the model (can be search spaces instead of fixed values).
        If None, model defaults are used. This is identical to passing an empty dictionary.
    """

    model_file_name = "model.pkl"
    model_info_name = "info.pkl"
    _oof_filename = "oof.pkl"

    # TODO: For which models should we override this parameter?
    _covariate_regressor_fit_time_fraction: float = 0.5
    default_max_time_limit_ratio: float = 0.9

    _supports_known_covariates: bool = False
    _supports_past_covariates: bool = False
    _supports_static_features: bool = False

    def __init__(
        self,
        path: Optional[str] = None,
        name: Optional[str] = None,
        hyperparameters: Optional[Dict[str, Any]] = None,
        freq: Optional[str] = None,
        prediction_length: int = 1,
        covariate_metadata: Optional[CovariateMetadata] = None,
        target: str = "target",
        quantile_levels: Sequence[float] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
        eval_metric: Union[str, TimeSeriesScorer, None] = None,
    ):
        self.name = name or re.sub(r"Model$", "", self.__class__.__name__)

        self.path_root = path
        if self.path_root is None:
            path_suffix = self.name
            # TODO: Would be ideal to not create dir, but still track that it is unique. However, this isn't possible
            # to do without a global list of used dirs or using UUID.
            path_cur = setup_outputdir(path=None, create_dir=True, path_suffix=path_suffix)
            self.path_root = path_cur.rsplit(self.name, 1)[0]
            logger.log(20, f"Warning: No path was specified for model, defaulting to: {self.path_root}")

        self.path = os.path.join(self.path_root, self.name)

        self.eval_metric = check_get_evaluation_metric(eval_metric, prediction_length=prediction_length)
        self.target: str = target
        self.covariate_metadata = covariate_metadata or CovariateMetadata()

        self.freq: Optional[str] = freq
        self.prediction_length: int = prediction_length
        self.quantile_levels: list[float] = list(quantile_levels)

        if not all(0 < q < 1 for q in self.quantile_levels):
            raise ValueError("Invalid quantile_levels specified. Quantiles must be between 0 and 1 (exclusive).")

        # We ensure that P50 forecast is always among the "raw" predictions generated by _predict.
        # We remove P50 from the final predictions if P50 wasn't present among the specified quantile_levels.
        if 0.5 not in self.quantile_levels:
            self.must_drop_median = True
            self.quantile_levels = sorted(set([0.5] + self.quantile_levels))
        else:
            self.must_drop_median = False

        self._oof_predictions: Optional[List[TimeSeriesDataFrame]] = None

        # user provided hyperparameters and extra arguments that are used during model training
        self._hyperparameters, self._extra_ag_args = self._check_and_split_hyperparameters(hyperparameters)

        self.fit_time: Optional[float] = None  # Time taken to fit in seconds (Training data)
        self.predict_time: Optional[float] = None  # Time taken to predict in seconds (Validation data)
        self.predict_1_time: Optional[float] = (
            None  # Time taken to predict 1 row of data in seconds (with batch size `predict_1_batch_size`)
        )
        self.val_score: Optional[float] = None  # Score with eval_metric (Validation data)

    def __repr__(self) -> str:
        return self.name

    def rename(self, name: str) -> None:
        if self.name is not None and len(self.name) > 0:
            self.path = os.path.join(os.path.dirname(self.path), name)
        else:
            self.path = os.path.join(self.path, name)
        self.name = name

    def set_contexts(self, path_context):
        self.path = path_context
        self.path_root = self.path.rsplit(self.name, 1)[0]

    @classmethod
    def _check_and_split_hyperparameters(
        cls, hyperparameters: Optional[Dict[str, Any]] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Given the user-specified hyperparameters, split into `hyperparameters` and `extra_ag_args`, intended
        to be used during model initialization.

        Parameters
        ----------
        hyperparameters : Optional[Dict[str, Any]], default = None
            The model hyperparameters dictionary provided to the model constructor.

        Returns
        -------
        hyperparameters: Dict[str, Any]
            Native model hyperparameters that are passed into the "inner model" AutoGluon wraps
        extra_ag_args: Dict[str, Any]
            Special auxiliary parameters that modify the model training process used by AutoGluon
        """
        hyperparameters = copy.deepcopy(hyperparameters) if hyperparameters is not None else dict()
        assert isinstance(hyperparameters, dict), (
            f"Invalid dtype for hyperparameters. Expected dict, but got {type(hyperparameters)}"
        )
        for k in hyperparameters.keys():
            if not isinstance(k, str):
                logger.warning(
                    f"Warning: Specified hyperparameter key is not of type str: {k} (type={type(k)}). "
                    f"There might be a bug in your configuration."
                )

        extra_ag_args = hyperparameters.pop(AG_ARGS_FIT, {})
        if not isinstance(extra_ag_args, dict):
            raise ValueError(
                f"Invalid hyperparameter type for `{AG_ARGS_FIT}`. Expected dict, but got {type(extra_ag_args)}"
            )
        return hyperparameters, extra_ag_args

    def save(self, path: Optional[str] = None, verbose: bool = True) -> str:
        if path is None:
            path = self.path

        # Save self._oof_predictions as a separate file, not model attribute
        if self._oof_predictions is not None:
            save_pkl.save(
                path=os.path.join(path, "utils", self._oof_filename),
                object=self._oof_predictions,
                verbose=verbose,
            )
        oof_predictions = self._oof_predictions
        self._oof_predictions = None

        file_path = os.path.join(path, self.model_file_name)
        save_pkl.save(path=file_path, object=self, verbose=verbose)

        self._oof_predictions = oof_predictions
        return path

    @classmethod
    def load(cls, path: str, reset_paths: bool = True, load_oof: bool = False, verbose: bool = True) -> Self:
        file_path = os.path.join(path, cls.model_file_name)
        model = load_pkl.load(path=file_path, verbose=verbose)
        if reset_paths:
            model.set_contexts(path)
        if load_oof and model._oof_predictions is None:
            model._oof_predictions = cls.load_oof_predictions(path=path, verbose=verbose)
        return model

    @classmethod
    def load_oof_predictions(cls, path: str, verbose: bool = True) -> List[TimeSeriesDataFrame]:
        """Load the cached OOF predictions from disk."""
        return load_pkl.load(path=os.path.join(path, "utils", cls._oof_filename), verbose=verbose)

    @property
    def supports_known_covariates(self) -> bool:
        return (
            self.get_hyperparameters().get("covariate_regressor") is not None
            or self.__class__._supports_known_covariates
        )

    @property
    def supports_past_covariates(self) -> bool:
        return self.__class__._supports_past_covariates

    @property
    def supports_static_features(self) -> bool:
        return (
            self.get_hyperparameters().get("covariate_regressor") is not None
            or self.__class__._supports_static_features
        )

    def get_oof_predictions(self):
        if self._oof_predictions is None:
            self._oof_predictions = self.load_oof_predictions(self.path)
        return self._oof_predictions

    def _get_default_hyperparameters(self) -> dict:
        return {}

    def get_hyperparameters(self) -> dict:
        """Get hyperparameters that will be passed to the "inner model" that AutoGluon wraps."""
        return {**self._get_default_hyperparameters(), **self._hyperparameters}

    def get_info(self) -> dict:
        """
        Returns a dictionary of numerous fields describing the model.
        """
        info = {
            "name": self.name,
            "model_type": type(self).__name__,
            "eval_metric": self.eval_metric,
            "fit_time": self.fit_time,
            "predict_time": self.predict_time,
            "freq": self.freq,
            "prediction_length": self.prediction_length,
            "quantile_levels": self.quantile_levels,
            "val_score": self.val_score,
            "hyperparameters": self.get_hyperparameters(),
            "covariate_metadata": self.covariate_metadata.to_dict(),
        }
        return info

    @classmethod
    def load_info(cls, path: str, load_model_if_required: bool = True) -> dict:
        # TODO: remove?
        load_path = os.path.join(path, cls.model_info_name)
        try:
            return load_pkl.load(path=load_path)
        except:
            if load_model_if_required:
                model = cls.load(path=path, reset_paths=True)
                return model.get_info()
            else:
                raise

    def _is_gpu_available(self) -> bool:
        return False

    @staticmethod
    def _get_system_resources() -> Dict[str, Any]:
        resource_manager = get_resource_manager()
        system_num_cpus = resource_manager.get_cpu_count()
        system_num_gpus = resource_manager.get_gpu_count()
        return {
            "num_cpus": system_num_cpus,
            "num_gpus": system_num_gpus,
        }

    def _get_model_base(self) -> Self:
        return self

    def persist(self) -> Self:
        """Ask the model to persist its assets in memory, i.e., to predict with low latency. In practice
        this is used for pretrained models that have to lazy-load model parameters to device memory at
        prediction time.
        """
        return self

    def _more_tags(self) -> dict:
        """Encode model properties using tags, similar to sklearn & autogluon.tabular.

        For more details, see `autogluon.core.models.abstract.AbstractModel._get_tags()` and https://scikit-learn.org/stable/_sources/developers/develop.rst.txt.

        List of currently supported tags:
        - allow_nan: Can the model handle data with missing values represented by np.nan?
        - can_refit_full: Does it make sense to retrain the model without validation data?
            See `autogluon.core.models.abstract._tags._DEFAULT_TAGS` for more details.
        - can_use_train_data: Can the model use train_data if it's provided to model.fit()?
        - can_use_val_data: Can the model use val_data if it's provided to model.fit()?
        """
        return {
            "allow_nan": False,
            "can_refit_full": False,
            "can_use_train_data": True,
            "can_use_val_data": False,
        }

    def get_params(self) -> dict:
        """Get the constructor parameters required for cloning this model object"""
        # We only use the user-provided hyperparameters for cloning. We cannot use the output of get_hyperparameters()
        # since it may contain search spaces that won't be converted to concrete values during HPO
        hyperparameters = self._hyperparameters.copy()
        if self._extra_ag_args:
            hyperparameters[AG_ARGS_FIT] = self._extra_ag_args.copy()

        return dict(
            path=self.path_root,
            name=self.name,
            eval_metric=self.eval_metric,
            hyperparameters=hyperparameters,
            freq=self.freq,
            prediction_length=self.prediction_length,
            quantile_levels=self.quantile_levels,
            covariate_metadata=self.covariate_metadata,
            target=self.target,
        )

    def convert_to_refit_full_via_copy(self) -> Self:
        # save the model as a new model on disk
        previous_name = self.name
        self.rename(self.name + REFIT_FULL_SUFFIX)
        refit_model_path = self.path
        self.save(path=self.path, verbose=False)

        self.rename(previous_name)

        refit_model = self.load(path=refit_model_path, verbose=False)
        refit_model.val_score = None
        refit_model.predict_time = None

        return refit_model

    def convert_to_refit_full_template(self) -> Self:
        """After calling this function, returned model should be able to be fit without `val_data`."""
        params = copy.deepcopy(self.get_params())

        # Remove 0.5 from quantile_levels so that the cloned model sets its must_drop_median correctly
        if self.must_drop_median:
            params["quantile_levels"].remove(0.5)

        if "hyperparameters" not in params:
            params["hyperparameters"] = dict()

        if AG_ARGS_FIT not in params["hyperparameters"]:
            params["hyperparameters"][AG_ARGS_FIT] = dict()

        params["name"] = params["name"] + REFIT_FULL_SUFFIX
        template = self.__class__(**params)

        return template


class AbstractTimeSeriesModel(TimeSeriesModelBase, TimeSeriesTunable, ABC):
    """Abstract base class for all time series models that take historical data as input and
    make predictions for the forecast horizon.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        name: Optional[str] = None,
        hyperparameters: Optional[Dict[str, Any]] = None,
        freq: Optional[str] = None,
        prediction_length: int = 1,
        covariate_metadata: Optional[CovariateMetadata] = None,
        target: str = "target",
        quantile_levels: Sequence[float] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
        eval_metric: Union[str, TimeSeriesScorer, None] = None,
    ):
        # TODO: make freq a required argument in AbstractTimeSeriesModel
        super().__init__(
            path=path,
            name=name,
            hyperparameters=hyperparameters,
            freq=freq,
            prediction_length=prediction_length,
            covariate_metadata=covariate_metadata,
            target=target,
            quantile_levels=quantile_levels,
            eval_metric=eval_metric,
        )
        self.target_scaler: Optional[TargetScaler]
        self.covariate_scaler: Optional[CovariateScaler]
        self.covariate_regressor: Optional[CovariateRegressor]

    def _initialize_transforms_and_regressor(self) -> None:
        self.target_scaler = get_target_scaler(self.get_hyperparameters().get("target_scaler"), target=self.target)
        self.covariate_scaler = get_covariate_scaler(
            self.get_hyperparameters().get("covariate_scaler"),
            covariate_metadata=self.covariate_metadata,
            use_static_features=self.supports_static_features,
            use_known_covariates=self.supports_known_covariates,
            use_past_covariates=self.supports_past_covariates,
        )
        self.covariate_regressor = get_covariate_regressor(
            self.get_hyperparameters().get("covariate_regressor"),
            target=self.target,
            covariate_metadata=self.covariate_metadata,
        )

    @property
    def allowed_hyperparameters(self) -> List[str]:
        """List of hyperparameters allowed by the model."""
        return ["target_scaler", "covariate_regressor"]

    def fit(
        self,
        train_data: TimeSeriesDataFrame,
        val_data: Optional[TimeSeriesDataFrame] = None,
        time_limit: Optional[float] = None,
        verbosity: int = 2,
        **kwargs,
    ) -> Self:
        """Fit timeseries model.

        Models should not override the `fit` method, but instead override the `_fit` method which
        has the same arguments.

        Other Parameters
        ----------------
        train_data : TimeSeriesDataFrame
            The training data provided in the library's `autogluon.timeseries.dataset.TimeSeriesDataFrame`
            format.
        val_data : TimeSeriesDataFrame, optional
            The validation data set in the same format as training data.
        time_limit : float, default = None
            Time limit in seconds to adhere to when fitting model.
            Ideally, model should early stop during fit to avoid going over the time limit if specified.
        num_cpus : int, default = 'auto'
            How many CPUs to use during fit.
            This is counted in virtual cores, not in physical cores.
            If 'auto', model decides.
        num_gpus : int, default = 'auto'
            How many GPUs to use during fit.
            If 'auto', model decides.
        verbosity : int, default = 2
            Verbosity levels range from 0 to 4 and control how much information is printed.
            Higher levels correspond to more detailed print statements (you can set verbosity = 0 to suppress warnings).
        **kwargs :
            Any additional fit arguments a model supports.

        Returns
        -------
        model: AbstractTimeSeriesModel
            The fitted model object
        """
        start_time = time.monotonic()
        self._initialize_transforms_and_regressor()

        if self.target_scaler is not None:
            train_data = self.target_scaler.fit_transform(train_data)

        if self.covariate_scaler is not None:
            train_data = self.covariate_scaler.fit_transform(train_data)

        if self.covariate_regressor is not None:
            covariate_regressor_time_limit = (
                self._covariate_regressor_fit_time_fraction * time_limit if time_limit is not None else None
            )
            self.covariate_regressor.fit(
                train_data,
                time_limit=covariate_regressor_time_limit,
                verbosity=verbosity - 1,
            )

        if self._get_tags()["can_use_train_data"]:
            if self.covariate_regressor is not None:
                train_data = self.covariate_regressor.transform(train_data)
            train_data, _ = self.preprocess(train_data, is_train=True)

        if self._get_tags()["can_use_val_data"] and val_data is not None:
            if self.target_scaler is not None:
                val_data = self.target_scaler.transform(val_data)
            if self.covariate_scaler is not None:
                val_data = self.covariate_scaler.transform(val_data)
            if self.covariate_regressor is not None:
                val_data = self.covariate_regressor.transform(val_data)
            val_data, _ = self.preprocess(val_data, is_train=False)

        if time_limit is not None:
            time_limit = time_limit - (time.monotonic() - start_time)
            time_limit = self._preprocess_time_limit(time_limit=time_limit)
            if time_limit <= 0:
                logger.warning(
                    f"\tWarning: Model has no time left to train, skipping model... (Time Left = {time_limit:.1f}s)"
                )
                raise TimeLimitExceeded

        self._fit(
            train_data=train_data,
            val_data=val_data,
            time_limit=time_limit,
            verbosity=verbosity,
            **(self._get_system_resources() | kwargs),
        )

        return self

    @abstractmethod
    def _fit(
        self,
        train_data: TimeSeriesDataFrame,
        val_data: Optional[TimeSeriesDataFrame] = None,
        time_limit: Optional[float] = None,
        num_cpus: Optional[int] = None,
        num_gpus: Optional[int] = None,
        verbosity: int = 2,
        **kwargs,
    ) -> None:
        """Private method for `fit`. See `fit` for documentation of arguments. Apart from
        the model training logic, `fit` additionally implements other logic such as keeping
        track of the time limit, etc.
        """
        pass

    # TODO: this check cannot be moved inside fit because of the complex way in which
    # MultiWindowBacktestingModel handles hyperparameter spaces during initialization.
    # Move inside fit() after refactoring MultiWindowBacktestingModel
    def _check_fit_params(self):
        # gracefully handle hyperparameter specifications if they are provided to fit instead
        if any(isinstance(v, space.Space) for v in self.get_hyperparameters().values()):
            raise ValueError(
                "Hyperparameter spaces provided to `fit`. Please provide concrete values "
                "as hyperparameters when initializing or use `hyperparameter_tune` instead."
            )

    def _log_unused_hyperparameters(self, extra_allowed_hyperparameters: list[str] | None = None) -> None:
        """Log a warning if unused hyperparameters were provided to the model."""
        allowed_hyperparameters = self.allowed_hyperparameters
        if extra_allowed_hyperparameters is not None:
            allowed_hyperparameters = allowed_hyperparameters + extra_allowed_hyperparameters

        unused_hyperparameters = [key for key in self.get_hyperparameters() if key not in allowed_hyperparameters]
        if len(unused_hyperparameters) > 0:
            logger.warning(
                f"{self.name} ignores following hyperparameters: {unused_hyperparameters}. "
                f"See the documentation for {self.name} for the list of supported hyperparameters."
            )

    def predict(
        self,
        data: TimeSeriesDataFrame,
        known_covariates: Optional[TimeSeriesDataFrame] = None,
        **kwargs,
    ) -> TimeSeriesDataFrame:
        """Given a dataset, predict the next `self.prediction_length` time steps.
        This method produces predictions for the forecast horizon *after* the individual time series.

        For example, if the data set includes 24 hour time series, of hourly data, starting from
        00:00 on day 1, and forecast horizon is set to 5. The forecasts are five time steps 00:00-04:00
        on day 2.

        Parameters
        ----------
        data: Union[TimeSeriesDataFrame, Dict[str, Optional[TimeSeriesDataFrame]]]
            The dataset where each time series is the "context" for predictions. For ensemble models that depend on
            the predictions of other models, this method may accept a dictionary of previous models' predictions.
        known_covariates : Optional[TimeSeriesDataFrame]
            A TimeSeriesDataFrame containing the values of the known covariates during the forecast horizon.

        Returns
        -------
        predictions: TimeSeriesDataFrame
            pandas dataframes with a timestamp index, where each input item from the input
            data is given as a separate forecast item in the dictionary, keyed by the `item_id`s
            of input items.
        """
        if self.target_scaler is not None:
            data = self.target_scaler.fit_transform(data)
        if self.covariate_scaler is not None:
            data = self.covariate_scaler.fit_transform(data)
            known_covariates = self.covariate_scaler.transform_known_covariates(known_covariates)
        if self.covariate_regressor is not None:
            data = self.covariate_regressor.fit_transform(data)

        data, known_covariates = self.preprocess(data, known_covariates, is_train=False)

        # FIXME: Set self.covariate_regressor=None so to avoid copying it across processes during _predict
        # FIXME: The clean solution is to convert all methods executed in parallel to @classmethod
        covariate_regressor = self.covariate_regressor
        self.covariate_regressor = None
        predictions = self._predict(data=data, known_covariates=known_covariates, **kwargs)
        self.covariate_regressor = covariate_regressor

        column_order = pd.Index(["mean"] + [str(q) for q in self.quantile_levels])
        if not predictions.columns.equals(column_order):
            predictions = predictions.reindex(columns=column_order)

        # "0.5" might be missing from the quantiles if self is a wrapper (MultiWindowBacktestingModel or ensemble)
        if "0.5" in predictions.columns:
            if self.eval_metric.optimized_by_median:
                predictions["mean"] = predictions["0.5"]
            if self.must_drop_median:
                predictions = predictions.drop("0.5", axis=1)

        if self.covariate_regressor is not None:
            if known_covariates is None:
                known_covariates = TimeSeriesDataFrame.from_data_frame(
                    pd.DataFrame(index=self.get_forecast_horizon_index(data), dtype="float32")
                )

            predictions = self.covariate_regressor.inverse_transform(
                predictions,
                known_covariates=known_covariates,
                static_features=data.static_features,
            )

        if self.target_scaler is not None:
            predictions = self.target_scaler.inverse_transform(predictions)
        return predictions

    def get_forecast_horizon_index(self, data: TimeSeriesDataFrame) -> pd.MultiIndex:
        """For each item in the dataframe, get timestamps for the next `prediction_length` time steps into the future."""
        return pd.MultiIndex.from_frame(
            make_future_data_frame(data, prediction_length=self.prediction_length, freq=self.freq)
        )

    @abstractmethod
    def _predict(
        self,
        data: TimeSeriesDataFrame,
        known_covariates: Optional[TimeSeriesDataFrame] = None,
        **kwargs,
    ) -> TimeSeriesDataFrame:
        """Private method for `predict`. See `predict` for documentation of arguments."""
        pass

    def _preprocess_time_limit(self, time_limit: float) -> float:
        original_time_limit = time_limit
        max_time_limit_ratio = self._extra_ag_args.get("max_time_limit_ratio", self.default_max_time_limit_ratio)
        max_time_limit = self._extra_ag_args.get("max_time_limit")

        time_limit *= max_time_limit_ratio

        if max_time_limit is not None:
            time_limit = min(time_limit, max_time_limit)

        if original_time_limit != time_limit:
            time_limit_og_str = f"{original_time_limit:.2f}s" if original_time_limit is not None else "None"
            time_limit_str = f"{time_limit:.2f}s" if time_limit is not None else "None"
            logger.debug(
                f"\tTime limit adjusted due to model hyperparameters: "
                f"{time_limit_og_str} -> {time_limit_str} "
                f"(ag.max_time_limit={max_time_limit}, "
                f"ag.max_time_limit_ratio={max_time_limit_ratio}"
            )

        return time_limit

    def _get_search_space(self):
        """Sets up default search space for HPO. Each hyperparameter which user did not specify is converted from
        default fixed value to default search space.
        """
        params = self._hyperparameters.copy()
        return params

    def _score_with_predictions(
        self,
        data: TimeSeriesDataFrame,
        predictions: TimeSeriesDataFrame,
    ) -> float:
        """Compute the score measuring how well the predictions align with the data."""
        return self.eval_metric.score(
            data=data,
            predictions=predictions,
            target=self.target,
        )

    def score(self, data: TimeSeriesDataFrame) -> float:
        """Return the evaluation scores for given metric and dataset. The last
        `self.prediction_length` time steps of each time series in the input data set
        will be held out and used for computing the evaluation score. Time series
        models always return higher-is-better type scores.

        Parameters
        ----------
        data: TimeSeriesDataFrame
            Dataset used for scoring.

        Returns
        -------
        score: float
            The computed forecast evaluation score on the last `self.prediction_length`
            time steps of each time series.
        """
        past_data, known_covariates = data.get_model_inputs_for_scoring(
            prediction_length=self.prediction_length, known_covariates_names=self.covariate_metadata.known_covariates
        )
        predictions = self.predict(past_data, known_covariates=known_covariates)
        return self._score_with_predictions(data=data, predictions=predictions)

    def score_and_cache_oof(
        self,
        val_data: TimeSeriesDataFrame,
        store_val_score: bool = False,
        store_predict_time: bool = False,
        **predict_kwargs,
    ) -> None:
        """Compute val_score, predict_time and cache out-of-fold (OOF) predictions."""
        past_data, known_covariates = val_data.get_model_inputs_for_scoring(
            prediction_length=self.prediction_length, known_covariates_names=self.covariate_metadata.known_covariates
        )
        predict_start_time = time.time()
        oof_predictions = self.predict(past_data, known_covariates=known_covariates, **predict_kwargs)
        self._oof_predictions = [oof_predictions]
        if store_predict_time:
            self.predict_time = time.time() - predict_start_time
        if store_val_score:
            self.val_score = self._score_with_predictions(val_data, oof_predictions)

    def preprocess(
        self,
        data: TimeSeriesDataFrame,
        known_covariates: Optional[TimeSeriesDataFrame] = None,
        is_train: bool = False,
        **kwargs,
    ) -> Tuple[TimeSeriesDataFrame, Optional[TimeSeriesDataFrame]]:
        """Method that implements model-specific preprocessing logic."""
        return data, known_covariates

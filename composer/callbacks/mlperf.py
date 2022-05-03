import json
import logging
import os
import platform
import subprocess
import sys
import warnings
from typing import Any, Dict, List, Optional, Sized

import torch
from torch.utils.data import DataLoader

import composer
from composer.core import State
from composer.core.callback import Callback
from composer.loggers import Logger
from composer.loggers.logger import LogLevel
from composer.utils import dist

try:
    import cpuinfo
    import psutil
    from mlperf_logging import mllog  # type: ignore (no pypi for ci)
    from mlperf_logging.mllog import constants  # type: ignore (no pypi for ci)

    mlperf_available = True
except ImportError:
    mlperf_available = False

# this callback only supports the following options:
BENCHMARKS = ("resnet",)
DIVISIONS = ("open",)
STATUS = ("onprem", "cloud", "preview")


def rank_zero() -> bool:
    return dist.get_global_rank() == 0


def require_mlperf_logging():
    if not mlperf_available:
        raise ImportError("""Please install with `pip install mosaicml[mlperf]` and also
                          install the logging library from: https://github.com/mlcommons/logging""")


class MLPerfCallback(Callback):
    """Creates a compliant results file for MLPerf Training benchmark.

    A submission folder structure will be created with the ``root_folder``
    as the base and the following directories::

        root_folder/
            results/
                [system_name]/
                    [benchmark]/
                        results_0.txt
                        results_1.txt
                        ...
            systems/
                [system_name].json

    A required systems description will be automatically generated,
    and best effort made to populate the fields, but should be manually
    checked prior to submission.

    Currently, only open division submissions are supported with this Callback.

    Example:

    .. code-block:: python

        from composer.callbacks import MLPerfCallback

        callback = MLPerfCallback(
            root_folder='/submission',
            index=0,
            metric_name='Accuracy',
            metric_label='eval',
            target='0.759',
        )

    During training, the metric found in ``state.current_metrics[metric_label][metric_name]``
    will be compared against the target criterion.

    .. note::

        This is currently an experimental logger, that has not been used (yet)
        to submit an actual result to MLPerf. Please use with caution.

    .. note::

        MLPerf submissions require clearing the system cache prior to any training run.
        By default, this callback does not clear the cache, as that is a system specific
        operation. To enable cache clearing, and thus pass the mlperf compliance checker,
        provide a ``cache_clear_cmd`` that will be executed with ``os.system``.

    Args:
        root_folder (str): The root submission folder
        index (int): The repetition index of this run. The filename created will be
            ``result_[index].txt``.
        benchmark (str, optional): Benchmark name. Currently only ``resnet`` supported.
        target (float, optional): The target metric before the mllogger marks the stop
            of the timing run. Default: ``0.759`` (resnet benchmark).
        division (str, optional): Division of submission. Currently only ``open`` division supported.
        metric_name (str, optional): name of the metric to compare against the target. Default: ``Accuracy``.
        metric_label (str, optional): label name. The metric will be accessed via ``state.current_metrics[metric_label][metric_name]``.
        submitter (str, optional): Submitting organization. Default: MosaicML.
        system_name (str, optional): Name of the system (e.g. 8xA100_composer). If
            not provided, system name will default to ``[world_size]x[device_name]_composer``,
            e.g. ``8xNVIDIA_A100_80GB_composer``.
        status (str, optional): Submission status. One of (onprem, cloud, or preview).
            Default: ``"onprem"``.
        cache_clear_cmd (str, optional): Command to invoke during the cache clear. This callback
            will call ``os.system(cache_clear_cmd)``. Default is disabled (None)
    """

    def __init__(
        self,
        root_folder: str,
        index: int,
        benchmark: str = 'resnet',
        target: float = 0.759,
        division: str = 'open',
        metric_name: str = 'Accuracy',
        metric_label: str = 'eval',
        submitter: str = "MosaicML",
        system_name: Optional[str] = None,
        status: str = "onprem",
        cache_clear_cmd: Optional[List[str]] = None,
    ) -> None:

        require_mlperf_logging()

        if benchmark not in BENCHMARKS:
            raise ValueError(f"benchmark: {benchmark} must be one of {BENCHMARKS}")
        if division not in DIVISIONS:
            raise ValueError(f"division: {division} must be one of {DIVISIONS}")
        if status not in STATUS:
            raise ValueError(f"status: {status} must be one of {STATUS}")

        self.mllogger = mllog.get_mllogger()
        self.target = target
        self.benchmark = benchmark
        self.target = target
        self.division = division
        self.submitter = submitter
        self.status = status
        self.cache_clear_cmd = cache_clear_cmd
        self.root_folder = root_folder
        self.metric_name = metric_name
        self.metric_label = metric_label
        self._file_handler = None

        self.system_desc = get_system_description(submitter, division, status, system_name)
        if system_name is None:
            system_name = self.system_desc['system_name']
        self.system_name = system_name

        # file paths to save the systems file, results file
        self.systems_path = os.path.join(root_folder, 'systems', f'{system_name}.json')
        self.filename = os.path.join(root_folder, 'results', system_name, benchmark, f'result_{index}.txt')

        # upload names for object store logging
        self.upload_name = '{run_name}' + f'/results/{system_name}/{benchmark}/result_{index}.txt'
        self.system_desc_upload_name = '{run_name}' + f'/systems/{system_name}.json'

        self.success = False

    def init(self, state: State, logger: Logger) -> None:

        # setup here requies access to rank, which is only available after
        # the trainer is initialized
        if dist.get_local_rank() == 0:
            self._create_submission_folders(self.root_folder, self.system_name, self.benchmark)
            with open(self.systems_path, 'w') as f:
                json.dump(self.system_desc, f, indent=4)

        dist.barrier()

        if os.path.exists(self.filename):
            raise FileExistsError(f'{self.filename} already exists.')

        self._file_handler = logging.FileHandler(self.filename)
        self._file_handler.setLevel(logging.INFO)
        self.mllogger.logger.addHandler(self._file_handler)

        if self.cache_clear_cmd is not None:
            subprocess.run(self.cache_clear_cmd, check=True, text=True)
            self.mllogger.start(key=mllog.constants.CACHE_CLEAR)
        else:
            warnings.warn("cache_clear_cmd was not provided. For a valid submission, please provide the command.")

        self.mllogger.start(key=mllog.constants.INIT_START)

        if rank_zero():
            self._log_dict({
                constants.SUBMISSION_BENCHMARK: self.benchmark,
                constants.SUBMISSION_DIVISION: self.division,
                constants.SUBMISSION_ORG: self.submitter,
                constants.SUBMISSION_PLATFORM: self.system_name,
                constants.SUBMISSION_STATUS: self.status,
            })

            # optionally, upload the system description file
            logger.file_artifact(LogLevel.FIT, self.system_desc_upload_name, self.systems_path)

    def _create_submission_folders(self, root_folder: str, system_name: str, benchmark: str):
        os.makedirs(root_folder, exist_ok=True)

        results_folder = os.path.join(root_folder, 'results')
        log_folder = os.path.join(root_folder, 'results', system_name)
        benchmark_folder = os.path.join(log_folder, benchmark)
        systems_folder = os.path.join(root_folder, 'systems')

        os.makedirs(results_folder, exist_ok=True)
        os.makedirs(log_folder, exist_ok=True)
        os.makedirs(benchmark_folder, exist_ok=True)
        os.makedirs(systems_folder, exist_ok=True)

    def _log_dict(self, data: Dict[str, Any]):
        for key, value in data.items():
            self.mllogger.event(key=key, value=value)

    def _get_accuracy(self, state: State):
        if self.metric_name not in state.current_metrics[self.metric_label]:
            raise ValueError('Accuracy must be a validation metric.')
        return state.current_metrics[self.metric_label][self.metric_name]

    def fit_start(self, state: State, logger: Logger) -> None:
        if rank_zero():

            if not isinstance(state.train_dataloader, DataLoader):
                raise TypeError("train dataloader must be a torch dataloader")
            if not isinstance(state.evaluators[0].dataloader.dataloader, DataLoader):
                raise TypeError("eval dataset must be a torch dataloader.")
            if state.train_dataloader.batch_size is None:
                raise ValueError("Batch size is required to be set for dataloader.")
            if len(state.evaluators) > 1:
                raise ValueError("Only one evaluator is supported for the MLPerfCallback.")
            if not isinstance(state.train_dataloader.dataset, Sized):
                raise TypeError("Train dataset must have __len__ property")
            if not isinstance(state.evaluators[0].dataloader.dataloader.dataset, Sized):
                raise TypeError("The eval dataset must have __len__ property")

            self._log_dict({
                constants.SEED: state.seed,
                constants.GLOBAL_BATCH_SIZE: state.train_dataloader.batch_size * dist.get_world_size(),
                constants.GRADIENT_ACCUMULATION_STEPS: state.grad_accum,
                constants.TRAIN_SAMPLES: len(state.train_dataloader.dataset),
                constants.EVAL_SAMPLES: len(state.evaluators[0].dataloader.dataloader.dataset)
            })

        self.mllogger.event(key=constants.INIT_STOP)

        dist.barrier()

        if rank_zero():
            self.mllogger.event(key=constants.RUN_START)

    def epoch_start(self, state: State, logger: Logger) -> None:
        if rank_zero():
            self.mllogger.event(key=constants.EPOCH_START, metadata={'epoch_num': state.timer.epoch.value})
            self.mllogger.event(key=constants.BLOCK_START,
                                metadata={
                                    'first_epoch_num': state.timer.epoch.value,
                                    'epoch_count': 1
                                })

    def epoch_end(self, state: State, logger: Logger) -> None:
        if rank_zero():
            self.mllogger.event(key=constants.EPOCH_STOP, metadata={'epoch_num': state.timer.epoch.value})
            logger.file_artifact(LogLevel.FIT, artifact_name=self.upload_name, file_path=self.filename)

    def eval_start(self, state: State, logger: Logger) -> None:
        if rank_zero():
            self.mllogger.event(key=constants.EVAL_START, metadata={'epoch_num': state.timer.epoch.value})

    def eval_end(self, state: State, logger: Logger) -> None:
        if rank_zero():
            accuracy = self._get_accuracy(state)

            self.mllogger.event(key=constants.EVAL_STOP, metadata={'epoch_num': state.timer.epoch.value})
            self.mllogger.event(key=constants.EVAL_ACCURACY,
                                value=accuracy,
                                metadata={'epoch_num': state.timer.epoch.value})
            self.mllogger.event(key=constants.BLOCK_STOP, metadata={'first_epoch_num': state.timer.epoch.value})

            if accuracy > self.target and not self.success:
                self.mllogger.event(key=constants.RUN_STOP, metadata={"status": "success"})
                self.mllogger.logger.removeHandler(self._file_handler)
                self.success = True  # only log once

    def close(self, state: State, logger: Logger) -> None:
        if self._file_handler is not None:
            self._file_handler.close()


def get_system_description(
    submitter: str,
    division: str,
    status: str,
    system_name: Optional[str] = None,
) -> Dict[str, str]:
    """Generates a valid system description.

    Make a best effort to auto-populate some of the fields, but should
    be manually checked prior to submission. The system name is
    auto-generated as "[world_size]x[device_name]_composer", e.g.
    "8xNVIDIA_A100_80GB_composer".

    Args:
        submitter (str): Name of the submitter organization
        division (str): Submission division (open, closed)
        status (str): system status (cloud, onprem, preview)

    Returns:
        system description as a dictionary
    """
    is_cuda = torch.cuda.is_available()
    cpu_info = cpuinfo.get_cpu_info()

    system_desc = {
        "submitter": submitter,
        "division": division,
        "status": status,
        "number_of_nodes": dist.get_world_size() / dist.get_local_world_size(),
        "host_processors_per_node": "",
        "host_processor_model_name": str(cpu_info.get('brand_raw', "CPU")),
        "host_processor_core_count": str(psutil.cpu_count(logical=False)),
        "host_processor_vcpu_count": "",
        "host_processor_frequency": "",
        "host_processor_caches": "",
        "host_processor_interconnect": "",
        "host_memory_capacity": "",
        "host_storage_type": "",
        "host_storage_capacity": "",
        "host_networking": "",
        "host_networking_topology": "",
        "host_memory_configuration": "",
        "accelerators_per_node": str(dist.get_local_world_size()) if is_cuda else "0",
        "accelerator_model_name": str(torch.cuda.get_device_name(None)) if is_cuda else "",
        "accelerator_host_interconnect": "",
        "accelerator_frequency": "",
        "accelerator_on-chip_memories": "",
        "accelerator_memory_configuration": "",
        "accelerator_memory_capacity": "",
        "accelerator_interconnect": "",
        "accelerator_interconnect_topology": "",
        "cooling": "",
        "hw_notes": "",
        "framework":
            f"PyTorch v{torch.__version__} and MosaicML composer v{composer.__version__}",  # type: ignore (third-party missing stub)
        "other_software_stack": {
            "cuda_version": torch.version.cuda if is_cuda else "",  # type: ignore (third-party missing stub)
            "composer_version": composer.__version__,
            "python_version": sys.version,
        },
        "operating_system": f"{platform.system()} {platform.release()}",
        "sw_notes": "",
    }

    if system_desc['number_of_nodes'] != 1:
        warnings.warn("Number of nodes > 1 not tested, proceed with caution.")

    if system_name is None:
        world_size = dist.get_world_size()
        if is_cuda:
            device_name = system_desc['accelerator_model_name']
        else:
            device_name = system_desc['host_processor_model_name']

        device_name = device_name.replace(' ', '_')
        system_name = f"{world_size}x{device_name}_composer"

    # default to system name as "[world_size]x[device_name]"
    # e.g. 8xNVIDIA_A100_80GB
    system_desc['system_name'] = system_name
    return system_desc
from abc import ABCMeta
import json
import os
from datetime import datetime
from pathlib import Path
import socket
import shutil
from typing import Optional, Dict
from uuid import UUID
import yaml

from pydantic import (
    BaseSettings,
    PyObject,
    validator,
    ValidationError,
)
from typing import List
from balsam.client import RESTClient, NotAuthenticatedError
from balsam.schemas import AllowedQueue
from balsam.platform.transfer import GlobusTransferInterface

from balsam.util import config_file_logging


class InvalidSettings(Exception):
    pass


def balsam_home():
    return Path.home().joinpath(".balsam")


def get_class_path(cls):
    return cls.__module__ + "." + cls.__name__


class ClientSettings(BaseSettings):
    api_root: str
    username: str
    client_class: PyObject = "balsam.client.BasicAuthRequestsClient"
    token: Optional[str] = None
    token_expiry: Optional[datetime] = None
    connect_timeout: float = 3.1
    read_timeout: float = 5.0
    retry_count: int = 3

    @validator("client_class")
    def client_type_is_correct(cls, v):
        if not issubclass(v, RESTClient):
            raise TypeError(f"client_class must subclass balsam.client.RESTClient")
        return v

    @staticmethod
    def settings_path():
        return balsam_home().joinpath("client.yml")

    @classmethod
    def load_from_home(cls):
        try:
            with open(cls.settings_path()) as fp:
                data = yaml.safe_load(fp)
        except FileNotFoundError:
            raise NotAuthenticatedError(
                f"Client credentials {cls.settings_path()} do not exist. "
                f"Please authenticate with `balsam login`."
            )
        return cls(**data)

    def save_to_home(self):
        data = self.dict()
        cls = data["client_class"]
        data["client_class"] = get_class_path(cls)

        settings_path = self.settings_path()
        if not settings_path.parent.is_dir():
            settings_path.parent.mkdir()
        if settings_path.exists():
            os.chmod(settings_path, 0o600)
        else:
            open(settings_path, "w").close()
            os.chmod(settings_path, 0o600)
        with open(settings_path, "w+") as fp:
            yaml.dump(data, fp, sort_keys=False, indent=4)

    def build_client(self):
        client = self.client_class(**self.dict(exclude={"client_class"}))
        return client


class LoggingConfig(BaseSettings):
    level: str = "DEBUG"
    format: str = "%(asctime)s|%(process)d|%(thread)d|%(levelname)8s|%(name)s:%(lineno)s] %(message)s"
    datefmt: str = "%d-%b-%Y %H:%M:%S"
    buffer_num_records: int = 1024
    flush_period: int = 30


class SchedulerSettings(BaseSettings):
    scheduler_class: PyObject = "balsam.platform.scheduler.CobaltScheduler"
    sync_period: int = 60
    allowed_queues: Dict[str, AllowedQueue] = {
        "default": AllowedQueue(
            max_nodes=4010, max_walltime=24 * 60, max_queued_jobs=20
        ),
        "debug-cache-quad": AllowedQueue(
            max_nodes=8, max_walltime=60, max_queued_jobs=1
        ),
    }
    allowed_projects: List[str] = ["datascience", "magstructsADSP"]
    optional_batch_job_params: Dict[str, str] = {"singularity_prime_cache": "no"}
    job_template_path: Path = Path("job-template.sh")


class QueueMaintainerSettings(BaseSettings):
    submit_period: int = 60
    submit_project: str = "local"
    submit_queue: str = "local"
    job_mode: str = "mpi"
    num_queued_jobs: int = 5
    num_nodes: int = 1
    wall_time_min: int = 1


class ProcessingSettings(BaseSettings):
    num_workers: int = 5
    prefetch_depth: int = 1000


class TransferSettings(BaseSettings):
    transfer_locations: Dict[str, str] = {
        "theta_dtn": "globus://08925f04-569f-11e7-bef8-22000b9a448b"
    }
    max_concurrent_transfers: int = 5
    globus_endpoint_id: Optional[UUID] = None
    transfer_batch_size: int = 100
    num_items_query_limit: int = 2000
    service_period: int = 5


class LauncherSettings(BaseSettings):
    idle_ttl_sec: int = 10
    delay_sec: int = 1
    error_tail_num_lines: int = 10
    max_concurrent_mpiruns: int = 1000
    compute_node: PyObject = "balsam.platform.compute_node.ThetaKNLNode"
    mpi_app_launcher: PyObject = "balsam.platform.app_run.ThetaAprun"
    local_app_launcher: PyObject = "balsam.platform.app_run.LocalAppRun"
    mpirun_allows_node_packing: bool = False
    serial_mode_prefetch_per_rank: int = 64
    serial_mode_startup_params: dict = {"cpu_affinity": "none"}


class Settings(BaseSettings):
    site_id: int = -1
    logging: LoggingConfig = LoggingConfig()
    filter_tags: Dict[str, str] = {"workflow": "test-1", "system": "H2O"}

    # Balsam service modules
    launcher: LauncherSettings = LauncherSettings()
    scheduler: Optional[SchedulerSettings] = SchedulerSettings()
    processing: Optional[ProcessingSettings] = ProcessingSettings()
    transfers: Optional[TransferSettings] = TransferSettings()
    queue_maintainer: Optional[QueueMaintainerSettings] = QueueMaintainerSettings()

    def save(self, path):
        with open(path, "w") as fp:
            fp.write(self.dump_yaml())

    def dump_yaml(self):
        return yaml.dump(
            json.loads(self.json()),
            sort_keys=False,
            indent=4,
        )

    @classmethod
    def load(cls, path):
        with open(path) as fp:
            raw_data = yaml.safe_load(fp)
        return cls(**raw_data)

    class Config:
        json_encoders = {
            type: get_class_path,
            ABCMeta: get_class_path,
        }


class SiteConfig:
    """
    Uses above settings to build components and provide dependencies
    No component should refer to external settings or set its own dependencies
    Instead, this class builds and injects needed settings/dependencies at runtime
    """

    def __init__(self, site_path=None, settings=None):
        self.site_path: Path = self.resolve_site_path(site_path)

        if settings is not None:
            if not isinstance(settings, Settings):
                raise ValueError(
                    f"If you're passing the settings kwarg, it must be an instance of balsam.config.Settings. "
                    "Otherwise, leave settings=None to auto-load the settings stored at BALSAM_SITE_PATH."
                )
            self.settings = settings
            return

        yaml_settings = self.site_path.joinpath("settings.yml")

        if not yaml_settings.is_file():
            raise FileNotFoundError(f"{site_path} must contain a settings.yml")
        try:
            self.settings = Settings.load(yaml_settings)
        except ValidationError as exc:
            raise InvalidSettings(f"{yaml_settings} is invalid:\n{exc}")
        self.client = ClientSettings.load_from_home().build_client()

    def build_services(self):
        from balsam.site.service import (
            SchedulerService,
            ProcessingService,
            QueueMaintainerService,
            TransferService,
        )

        services = []

        if self.settings.scheduler:
            scheduler_service = SchedulerService(
                client=self.client,
                site_id=self.settings.site_id,
                submit_directory=self.job_path,
                filter_tags=self.settings.filter_tags,
                **dict(self.settings.scheduler),  # does not convert sub-models to dicts
            )
            services.append(scheduler_service)

        if self.settings.queue_maintainer:
            queue_maintainer = QueueMaintainerService(
                client=self.client,
                site_id=self.settings.site_id,
                filter_tags=self.settings.filter_tags,
                **dict(
                    self.settings.queue_maintainer
                ),  # does not convert sub-models to dicts
            )
            services.append(queue_maintainer)

        if self.settings.processing:
            processing_service = ProcessingService(
                client=self.client,
                site_id=self.site_id,
                data_path=self.data_path,
                apps_path=self.apps_path,
                filter_tags=self.settings.filter_tags,
                **dict(
                    self.settings.processing
                ),  # does not convert sub-models to dicts
            )
            services.append(processing_service)

        if self.settings.transfers:
            transfer_settings = dict(self.settings.transfers)
            transfer_interfaces = {}
            endpoint_id = transfer_settings.pop("globus_endpoint_id")
            if endpoint_id:
                transfer_interfaces["globus"] = GlobusTransferInterface(endpoint_id)
            transfer_service = TransferService(
                client=self.client,
                site_id=self.settings.site_id,
                data_path=self.data_path,
                transfer_interfaces=transfer_interfaces,
                **dict(transfer_settings),
            )
            services.append(transfer_service)
        return services

    @staticmethod
    def load_default_config_dirs():
        """
        Get list of pre-configured Site directories for new site setup
        """
        defaults_dir = Path(__file__).parent.joinpath("defaults")
        default_settings_files = defaults_dir.glob("*/settings.yml")
        return [p.parent for p in default_settings_files]

    @classmethod
    def new_site_setup(cls, site_path, default_site_path, hostname=None):
        """
        Creates a new site directory, registers Site
        with Balsam API, and writes default settings.yml into
        Site directory
        """
        site_path = Path(site_path)
        site_path.mkdir(exist_ok=False, parents=True)
        site_path.joinpath(".balsam-site").touch()

        defaults_path = default_site_path.joinpath("settings.yml")
        try:
            settings = Settings.load(defaults_path)
        except ValidationError as exc:
            shutil.rmtree(site_path)
            raise InvalidSettings(f"{defaults_path} is invalid:\n{exc}")

        client = ClientSettings.load_from_home().build_client()
        site = client.Site.objects.create(
            hostname=socket.gethostname() if hostname is None else hostname,
            path=site_path,
        )
        settings.site_id = site.id
        settings.save(path=site_path.joinpath("settings.yml"))

        try:
            cf = cls(site_path=site_path, settings=settings)
            for path in [cf.log_path, cf.job_path, cf.data_path]:
                path.mkdir(exist_ok=False)
            shutil.copytree(
                src=default_site_path.joinpath("apps"),
                dst=cf.apps_path,
            )
            shutil.copy(
                src=default_site_path.joinpath(settings.scheduler.job_template_path),
                dst=cf.site_path,
            )
        except FileNotFoundError:
            site.delete()
            shutil.rmtree(site_path)
            raise

    @staticmethod
    def resolve_site_path(site_path=None) -> Path:
        # Site determined from either passed argument, environ,
        # or walking up parent directories, in that order
        site_path = (
            site_path
            or os.environ.get("BALSAM_SITE_PATH")
            or SiteConfig.search_site_dir()
        )
        if site_path is None:
            raise ValueError(
                "Initialize SiteConfig with a `site_path` or set env BALSAM_SITE_PATH "
                "to a Balsam site directory containing a settings.py file."
            )

        site_path = Path(site_path).resolve()
        if not site_path.is_dir():
            raise FileNotFoundError(
                f"BALSAM_SITE_PATH {site_path} must point to an existing Balsam site directory"
            )
        if not site_path.joinpath(".balsam-site").is_file():
            raise FileNotFoundError(
                f"BALSAM_SITE_PATH {site_path} is not a valid Balsam site directory "
                f"(does not contain a .balsam-site file)"
            )
        os.environ["BALSAM_SITE_PATH"] = str(site_path)
        return site_path

    @staticmethod
    def search_site_dir():
        check_dir = Path.cwd()
        while check_dir.as_posix() != "/":
            if check_dir.joinpath(".balsam-site").is_file():
                return check_dir
            check_dir = check_dir.parent

    def __getattr__(self, item):
        return getattr(self.settings, item)

    @property
    def apps_path(self):
        return self.site_path.joinpath("apps")

    @property
    def log_path(self):
        return self.site_path.joinpath("log")

    @property
    def job_path(self):
        return self.site_path.joinpath("qsubmit")

    @property
    def data_path(self):
        return self.site_path.joinpath("data")

    def enable_logging(self, basename, filename=None):
        if filename is None:
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            filename = f"{basename}_{ts}.log"
        log_path = self.log_path.joinpath(filename)
        config_file_logging(
            filename=log_path,
            **self.settings.logging.dict(),
        )
        return {"filename": log_path, **self.settings.logging.dict()}

"""Render selected bootstrap templates with the Python template environment."""

import json
import os
import sys

from jinja2 import Environment, FileSystemLoader


def default_argument(args, kwargs):
    """Return a default passed positionally or by keyword."""
    if "default" in kwargs:
        return kwargs["default"]
    for argument in args:
        if isinstance(argument, dict) and "default" in argument:
            return argument["default"]
    return None


class Values:
    """Expose the config getter methods used by bootstrap templates."""

    def __init__(self, flags):
        self.flags = flags
        self.strings = {
            "cluster.cluster_name": "sample-cluster",
            "cluster.cluster_s3_bucket": "sample-bucket",
            "cluster.home_dir": "/apps/sample-cluster",
            "cluster.aws.region": "us-east-2",
            "cluster.aws.account_id": "123456789012",
            "cluster.aws.dns_suffix": "amazonaws.com",
            "directoryservice.provider": "activedirectory" if flags["directory"] else "openldap",
            "directoryservice.ad_short_name": "EXAMPLE",
            "directoryservice.hostname": "directory.example.invalid",
            "directoryservice.ldap_base": "dc=example,dc=invalid",
            "directoryservice.name": "example",
            "scheduler.provider": "openpbs",
            "virtual-desktop-controller.events_sqs_queue_url": "https://example.invalid/queue",
            "virtual-desktop-controller.dcv_broker.gateway_communication_port": "8445",
        }
        self.shared_storage = {
            "apps": {
                "provider": "efs",
                "mount_dir": "/apps",
                "mount_options": "defaults",
                "efs": {"dns": "fs-apps.example.invalid"},
            },
            "scratch": {
                "provider": "fsx_lustre",
                "mount_dir": "/fsx",
                "mount_options": "flock",
                "fsx_lustre": {
                    "dns": "fsx.example.invalid",
                    "mount_name": "fsx",
                },
            },
        }

    def get_string(self, key, *args, **kwargs):
        value = self.strings.get(key)
        if value is not None:
            return value
        default = default_argument(args, kwargs)
        return "configured" if default is None else default

    def get_bool(self, key, *args, **kwargs):
        return default_argument(args, kwargs) or False

    def get_list(self, key, *args, **kwargs):
        return default_argument(args, kwargs) or []

    def get_int(self, key, *args, **kwargs):
        return default_argument(args, kwargs) or 1

    def get_config(self, key, *args, **kwargs):
        if key == "shared-storage":
            return self.shared_storage
        return default_argument(args, kwargs) or {}

    def get_cluster_internal_endpoint(self, *args, **kwargs):
        return "https://example.invalid"

    def get_cluster_external_endpoint(self, *args, **kwargs):
        return "https://example.invalid"


class Utils:
    """Provide deterministic utility methods called by the templates."""

    def to_json(self, value, *args, **kwargs):
        return json.dumps(value, separators=(",", ":"))

    def to_yaml(self, value, *args, **kwargs):
        return json.dumps(value, separators=(",", ":")) + "\n"

    def generate_password(self, *args, **kwargs):
        return "sample-password"

    def short_uuid(self, *args, **kwargs):
        return "sample-uuid"


class Attributes:
    """Allow template attributes to be constructed from a mapping."""

    def __init__(self, values):
        for key, value in values.items():
            setattr(self, key, Attributes(value) if isinstance(value, dict) else value)


class ScratchStorageSize:
    """Expose the numeric helpers used by the scratch storage template."""

    value = 0

    def int_val(self):
        return 0


class Job(Attributes):
    """Provide the job methods referenced by compute-node templates."""

    def is_persistent_capacity(self):
        return False

    def is_shared_capacity(self):
        return False

    def get_compute_stack(self):
        return "sample-compute-stack"


class Context:
    """Expose deterministic values and predicates for every branch family."""

    aws_region = "us-east-2"
    base_os = "rhel9"
    module_name = "virtual-desktop-controller"
    module_id = "vdc"
    module_set = "default"
    module_version = "26.09.0"
    cluster_s3_bucket = "sample-bucket"
    cluster_name = "sample-cluster"
    cluster_home_dir = "/apps/sample-cluster"
    app_deploy_dir = "/opt/idea/app"
    https_proxy = ""
    no_proxy = ""

    def __init__(self, flags, base_os=None):
        self.flags = flags
        if base_os is not None:
            self.base_os = base_os
        self.config = Values(flags)
        self.utils = Utils()
        self.vars = Attributes(
            {
                "idea_session_id": "sample-session",
                "session_owner": "sample-user",
                "dcv_host_ready_message": "sample-ready",
                "controller_package_uri": "s3://sample-bucket/release.tar.gz",
                "app_package_uri": "s3://sample-bucket/release.tar.gz",
                "ami_dir": "/apps/sample-cluster/ami",
                "ami_name": "sample-ami",
                "bedrock_env": {},
                "bedrock_model_messages": [],
                "enabled_drivers": ["fsx_lustre"] if flags["fsx"] else [],
                "session": {"type": "console"},
                "job": {
                    "job_name": "sample-job",
                    "job_id": "sample-job-id",
                    "job_uid": "1000",
                    "job_group": "sample-group",
                    "owner": "sample-owner",
                    "owner_email": "sample-owner@example.invalid",
                    "project": "sample-project",
                    "queue": "normal",
                    "scaling_mode": "single_job",
                    "params": {
                        "fsx_lustre": {
                            "enabled": flags["fsx"],
                            "existing_fsx": "fsx.example.invalid",
                        },
                        "enable_efa_support": False,
                        "enable_ht_support": False,
                    },
                },
                "job_directory": "/apps/sample-cluster/jobs/sample-job",
            }
        )
        self.vars.job = Job(vars(self.vars.job))
        self.vars.job.params.scratch_storage_size = ScratchStorageSize()
        self.vars.bedrock_env = {}

    def get_cloudwatch_agent_config(self, *args, **kwargs):
        return None

    def get_custom_aws_tags(self, *args, **kwargs):
        return []

    def has_storage_provider(self, provider, *args, **kwargs):
        return self.flags["fsx"] and provider in ("fsx_lustre", "fsx_cache")

    def is_metrics_provider_prometheus(self, *args, **kwargs):
        return self.flags["metrics"]

    def is_prometheus_exporter_enabled(self, *args, **kwargs):
        return self.flags["metrics"]

    def get_prometheus_config(self, *args, **kwargs):
        return {"global": {"scrape_interval": "15s"}} if self.flags["metrics"] else None

    def is_gpu_instance_type(self, *args, **kwargs):
        return self.flags["gpu"]

    def is_nvidia_gpu(self, *args, **kwargs):
        return self.flags["gpu"]

    def job_has_param(self, *args, **kwargs):
        return False

    def fail_on_missing_gpu_driver(self, *args, **kwargs):
        return False

    def get_nvidia_gpu_driver_version(self, *args, **kwargs):
        return "555.42.02"

    def eval_shared_storage_scope(self, shared_storage, *args, **kwargs):
        return self.flags["storageScope"]


def render_context(request):
    """Build the top-level context required by the selected template tree."""
    if request["sourceKind"] == "config":
        return {
            "aws_region": "us-east-2",
            "cluster_name": "sample-cluster",
            "metrics_provider": request["metricsProvider"],
        }
    return {"context": Context(request["flags"], request.get("baseOs"))}


def main():
    """Read one render request and return rendered bytes or errors."""
    request = json.load(sys.stdin)
    scratch_directory = os.environ["JINJA_BRANCH_SCRATCH"]
    source_directory = os.path.join(scratch_directory, "templates")
    for name, source in request["sources"].items():
        if os.path.isabs(name) or ".." in name.split(os.path.sep):
            raise ValueError(f"invalid template path: {name}")
        destination = os.path.join(source_directory, name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w", encoding="utf-8") as file:
            file.write(source)
    environment = Environment(
        loader=FileSystemLoader(source_directory),
        autoescape=False,
        keep_trailing_newline=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    environment.globals.update({"True": True, "False": False, "None": None})
    rendered = {}
    for name in request["templates"]:
        try:
            rendered[name] = {"text": environment.get_template(name).render(**render_context(request))}
        except Exception as error:
            rendered[name] = {"error": f"{type(error).__name__}: {error}"}
    print(json.dumps(rendered, sort_keys=True))


if __name__ == "__main__":
    main()

import json
from unittest import mock

import pytest
from dagster import execute_job, job, op
from dagster._config import process_config, resolve_to_config_type
from dagster._core.definitions.reconstruct import reconstructable
from dagster._core.errors import DagsterUnmetExecutorRequirementsError
from dagster._core.execution.api import create_execution_plan
from dagster._core.execution.context.system import PlanData, PlanOrchestrationContext
from dagster._core.execution.context_creation_pipeline import create_context_free_log_manager
from dagster._core.execution.retries import RetryMode
from dagster._core.executor.init import InitExecutorContext
from dagster._core.executor.step_delegating.step_handler.base import StepHandlerContext
from dagster._core.storage.fs_io_manager import fs_io_manager
from dagster._core.test_utils import create_run_for_test, environ, instance_for_test
from dagster._grpc.types import ExecuteStepArgs
from dagster_k8s.container_context import K8sContainerContext
from dagster_k8s.executor import _K8S_EXECUTOR_CONFIG_SCHEMA, K8sStepHandler, k8s_job_executor
from dagster_k8s.job import UserDefinedDagsterK8sConfig


@job(
    executor_def=k8s_job_executor,
    resource_defs={"io_manager": fs_io_manager},
)
def bar():
    @op
    def foo():
        return 1

    foo()


RESOURCE_TAGS = {
    "limits": {"cpu": "500m", "memory": "2560Mi"},
    "requests": {"cpu": "250m", "memory": "64Mi"},
}

OTHER_RESOURCE_TAGS = {
    "limits": {"cpu": "1000m", "memory": "1280Mi"},
    "requests": {"cpu": "500m", "memory": "128Mi"},
}


VOLUME_MOUNTS_TAGS = [{"name": "volume1", "mount_path": "foo/bar", "sub_path": "file.txt"}]

OTHER_VOLUME_MOUNTS_TAGS = [{"name": "volume2", "mount_path": "baz/quux", "sub_path": "voom.txt"}]


@job(
    executor_def=k8s_job_executor,
    resource_defs={"io_manager": fs_io_manager},
)
def bar_with_resources():
    expected_resources = RESOURCE_TAGS
    user_defined_k8s_config_with_resources = UserDefinedDagsterK8sConfig(
        container_config={"resources": expected_resources},
    )
    user_defined_k8s_config_with_resources_json = json.dumps(
        user_defined_k8s_config_with_resources.to_dict()
    )

    @op(tags={"dagster-k8s/config": user_defined_k8s_config_with_resources_json})
    def foo():
        return 1

    foo()


@job(
    executor_def=k8s_job_executor,
    resource_defs={"io_manager": fs_io_manager},
    tags={
        "dagster-k8s/config": {
            "container_config": {
                "resources": RESOURCE_TAGS,
                "volume_mounts": VOLUME_MOUNTS_TAGS,
            }
        }
    },
)
def bar_with_tags_in_job_and_op():
    expected_resources = RESOURCE_TAGS
    user_defined_k8s_config_with_resources = UserDefinedDagsterK8sConfig(
        container_config={"resources": expected_resources},
    )
    json.dumps(user_defined_k8s_config_with_resources.to_dict())

    @op(tags={"dagster-k8s/config": {"container_config": {"resources": OTHER_RESOURCE_TAGS}}})
    def foo():
        return 1

    foo()


@job(
    executor_def=k8s_job_executor,
    resource_defs={"io_manager": fs_io_manager},
)
def bar_with_images():
    # Construct Dagster op tags with user defined k8s config.
    user_defined_k8s_config_with_image = UserDefinedDagsterK8sConfig(
        container_config={"image": "new-image"},
    )
    user_defined_k8s_config_with_image_json = json.dumps(
        user_defined_k8s_config_with_image.to_dict()
    )

    @op(tags={"dagster-k8s/config": user_defined_k8s_config_with_image_json})
    def foo():
        return 1

    foo()


@pytest.fixture
def python_origin_with_container_context():
    container_context_config = {
        "k8s": {
            "env_vars": ["BAZ_TEST"],
            "resources": {
                "requests": {"cpu": "256m", "memory": "128Mi"},
                "limits": {"cpu": "1000m", "memory": "2000Mi"},
            },
            "scheduler_name": "my-other-scheduler",
        }
    }

    python_origin = reconstructable(bar).get_python_origin()
    return python_origin._replace(
        repository_origin=python_origin.repository_origin._replace(
            container_context=container_context_config,
        )
    )


def test_requires_k8s_launcher_fail():
    with instance_for_test() as instance:
        with pytest.raises(
            DagsterUnmetExecutorRequirementsError,
            match="This engine is only compatible with a K8sRunLauncher",
        ):
            execute_job(reconstructable(bar), instance=instance, raise_on_error=True)


def _get_executor(instance, pipeline, executor_config=None):
    process_result = process_config(
        resolve_to_config_type(_K8S_EXECUTOR_CONFIG_SCHEMA),
        executor_config or {},
    )
    assert process_result.success, str(process_result.errors)

    return k8s_job_executor.executor_creation_fn(
        InitExecutorContext(
            job=pipeline,
            executor_def=k8s_job_executor,
            executor_config=process_result.value,
            instance=instance,
        )
    )


def _step_handler_context(pipeline, pipeline_run, instance, executor):
    execution_plan = create_execution_plan(pipeline)
    log_manager = create_context_free_log_manager(instance, pipeline_run)

    plan_context = PlanOrchestrationContext(
        plan_data=PlanData(
            job=pipeline,
            dagster_run=pipeline_run,
            instance=instance,
            execution_plan=execution_plan,
            raise_on_error=True,
            retry_mode=RetryMode.DISABLED,
        ),
        log_manager=log_manager,
        executor=executor,
        output_capture=None,
    )

    execute_step_args = ExecuteStepArgs(
        reconstructable(bar).get_python_origin(), pipeline_run.run_id, ["foo"]
    )

    return StepHandlerContext(
        instance=instance,
        plan_context=plan_context,
        steps=execution_plan.steps,
        execute_step_args=execute_step_args,
    )


def test_executor_init(k8s_run_launcher_instance):
    resources = {
        "requests": {"memory": "64Mi", "cpu": "250m"},
        "limits": {"memory": "128Mi", "cpu": "500m"},
    }

    executor = _get_executor(
        k8s_run_launcher_instance,
        reconstructable(bar),
        {
            "env_vars": ["FOO_TEST"],
            "resources": resources,
            "scheduler_name": "my-scheduler",
        },
    )

    run = create_run_for_test(
        k8s_run_launcher_instance,
        job_name="bar",
        job_code_origin=reconstructable(bar).get_python_origin(),
    )

    step_handler_context = _step_handler_context(
        pipeline=reconstructable(bar),
        pipeline_run=run,
        instance=k8s_run_launcher_instance,
        executor=executor,
    )

    # env vars from both launcher and the executor

    assert sorted(
        executor._step_handler._get_container_context(  # noqa: SLF001  # noqa: SLF001
            step_handler_context
        ).env_vars
    ) == sorted(
        [
            "FOO_TEST",
            "BAR_TEST",
        ]
    )

    assert sorted(
        executor._step_handler._get_container_context(  # noqa: SLF001  # noqa: SLF001
            step_handler_context
        ).resources
    ) == sorted(resources)

    assert (
        executor._step_handler._get_container_context(  # noqa: SLF001  # noqa: SLF001
            step_handler_context
        ).scheduler_name
        == "my-scheduler"
    )


def test_executor_init_container_context(
    k8s_run_launcher_instance, python_origin_with_container_context
):
    executor = _get_executor(
        k8s_run_launcher_instance,
        reconstructable(bar),
        {"env_vars": ["FOO_TEST"], "max_concurrent": 4},
    )

    run = create_run_for_test(
        k8s_run_launcher_instance,
        job_name="bar",
        job_code_origin=python_origin_with_container_context,
    )

    step_handler_context = _step_handler_context(
        pipeline=reconstructable(bar),
        pipeline_run=run,
        instance=k8s_run_launcher_instance,
        executor=executor,
    )

    # env vars from both launcher and the executor

    assert sorted(
        executor._step_handler._get_container_context(  # noqa: SLF001  # noqa: SLF001
            step_handler_context
        ).env_vars
    ) == sorted(
        [
            "BAR_TEST",
            "FOO_TEST",
            "BAZ_TEST",
        ]
    )
    assert executor._max_concurrent == 4  # noqa: SLF001
    assert sorted(
        executor._step_handler._get_container_context(  # noqa: SLF001  # noqa: SLF001
            step_handler_context
        ).resources
    ) == sorted(
        python_origin_with_container_context.repository_origin.container_context["k8s"]["resources"]
    )

    assert (
        executor._step_handler._get_container_context(  # noqa: SLF001  # noqa: SLF001
            step_handler_context
        ).scheduler_name
        == "my-other-scheduler"
    )


@pytest.fixture
def k8s_instance(kubeconfig_file):
    default_config = {
        "service_account_name": "dagit-admin",
        "instance_config_map": "dagster-instance",
        "postgres_password_secret": "dagster-postgresql-secret",
        "dagster_home": "/opt/dagster/dagster_home",
        "job_image": "fake_job_image",
        "load_incluster_config": False,
        "kubeconfig_file": kubeconfig_file,
    }
    with instance_for_test(
        overrides={
            "run_launcher": {
                "module": "dagster_k8s",
                "class": "K8sRunLauncher",
                "config": default_config,
            }
        }
    ) as instance:
        yield instance


def test_step_handler(kubeconfig_file, k8s_instance):
    mock_k8s_client_batch_api = mock.MagicMock()
    handler = K8sStepHandler(
        image="bizbuz",
        container_context=K8sContainerContext(
            namespace="foo",
        ),
        load_incluster_config=False,
        kubeconfig_file=kubeconfig_file,
        k8s_client_batch_api=mock_k8s_client_batch_api,
    )

    run = create_run_for_test(
        k8s_instance,
        job_name="bar",
        job_code_origin=reconstructable(bar).get_python_origin(),
    )
    list(
        handler.launch_step(
            _step_handler_context(
                pipeline=reconstructable(bar),
                pipeline_run=run,
                instance=k8s_instance,
                executor=_get_executor(
                    k8s_instance,
                    reconstructable(bar),
                ),
            )
        )
    )

    # Check that user defined k8s config was passed down to the k8s job.
    mock_method_calls = mock_k8s_client_batch_api.method_calls
    assert len(mock_method_calls) > 0
    method_name, _args, kwargs = mock_method_calls[0]
    assert method_name == "create_namespaced_job"
    assert kwargs["body"].spec.template.spec.containers[0].image == "bizbuz"


def test_step_handler_user_defined_config(kubeconfig_file, k8s_instance):
    mock_k8s_client_batch_api = mock.MagicMock()
    handler = K8sStepHandler(
        image="bizbuz",
        container_context=K8sContainerContext(
            namespace="foo",
            env_vars=["FOO_TEST"],
            resources={
                "requests": {"cpu": "128m", "memory": "64Mi"},
                "limits": {"cpu": "500m", "memory": "1000Mi"},
            },
        ),
        load_incluster_config=False,
        kubeconfig_file=kubeconfig_file,
        k8s_client_batch_api=mock_k8s_client_batch_api,
    )

    with environ({"FOO_TEST": "bar"}):
        run = create_run_for_test(
            k8s_instance,
            job_name="bar",
            job_code_origin=reconstructable(bar_with_resources).get_python_origin(),
        )
        list(
            handler.launch_step(
                _step_handler_context(
                    pipeline=reconstructable(bar_with_resources),
                    pipeline_run=run,
                    instance=k8s_instance,
                    executor=_get_executor(
                        k8s_instance,
                        reconstructable(bar_with_resources),
                    ),
                )
            )
        )

        # Check that user defined k8s config was passed down to the k8s job.
        mock_method_calls = mock_k8s_client_batch_api.method_calls
        assert len(mock_method_calls) > 0
        method_name, _args, kwargs = mock_method_calls[0]
        assert method_name == "create_namespaced_job"
        assert kwargs["body"].spec.template.spec.containers[0].image == "bizbuz"
        job_resources = kwargs["body"].spec.template.spec.containers[0].resources.to_dict()
        job_resources.pop("claims", None)
        assert job_resources == RESOURCE_TAGS

        env_vars = {
            env.name: env.value for env in kwargs["body"].spec.template.spec.containers[0].env
        }
        assert env_vars["FOO_TEST"] == "bar"


def test_step_handler_image_override(kubeconfig_file, k8s_instance):
    mock_k8s_client_batch_api = mock.MagicMock()
    handler = K8sStepHandler(
        image="bizbuz",
        container_context=K8sContainerContext(namespace="foo"),
        load_incluster_config=False,
        kubeconfig_file=kubeconfig_file,
        k8s_client_batch_api=mock_k8s_client_batch_api,
    )

    run = create_run_for_test(
        k8s_instance,
        job_name="bar",
        job_code_origin=reconstructable(bar_with_images).get_python_origin(),
    )
    list(
        handler.launch_step(
            _step_handler_context(
                pipeline=reconstructable(bar_with_images),
                pipeline_run=run,
                instance=k8s_instance,
                executor=_get_executor(
                    k8s_instance,
                    reconstructable(bar_with_images),
                ),
            )
        )
    )

    # Check that user defined k8s config was passed down to the k8s job.
    mock_method_calls = mock_k8s_client_batch_api.method_calls
    assert len(mock_method_calls) > 0
    method_name, _args, kwargs = mock_method_calls[0]
    assert method_name == "create_namespaced_job"
    assert kwargs["body"].spec.template.spec.containers[0].image == "new-image"


def test_step_handler_with_container_context(
    kubeconfig_file, k8s_instance, python_origin_with_container_context
):
    mock_k8s_client_batch_api = mock.MagicMock()
    handler = K8sStepHandler(
        image="bizbuz",
        container_context=K8sContainerContext(
            namespace="foo",
            env_vars=["FOO_TEST"],
        ),
        load_incluster_config=False,
        kubeconfig_file=kubeconfig_file,
        k8s_client_batch_api=mock_k8s_client_batch_api,
    )

    with environ({"FOO_TEST": "bar", "BAZ_TEST": "blergh"}):
        # Additional env vars come from container context on the run
        run = create_run_for_test(
            k8s_instance,
            job_name="bar",
            job_code_origin=python_origin_with_container_context,
        )
        list(
            handler.launch_step(
                _step_handler_context(
                    pipeline=reconstructable(bar),
                    pipeline_run=run,
                    instance=k8s_instance,
                    executor=_get_executor(
                        k8s_instance,
                        reconstructable(bar),
                    ),
                )
            )
        )

        # Check that user defined k8s config was passed down to the k8s job.
        mock_method_calls = mock_k8s_client_batch_api.method_calls
        assert len(mock_method_calls) > 0
        method_name, _args, kwargs = mock_method_calls[0]
        assert method_name == "create_namespaced_job"
        assert kwargs["body"].spec.template.spec.containers[0].image == "bizbuz"

        envs = {env.name: env.value for env in kwargs["body"].spec.template.spec.containers[0].env}

        assert envs["FOO_TEST"] == "bar"
        assert envs["BAZ_TEST"] == "blergh"


def test_step_raw_k8s_config_inheritance(
    k8s_run_launcher_instance, python_origin_with_container_context
):
    container_context_config = {
        "k8s": {
            "run_k8s_config": {"container_config": {"volume_mounts": OTHER_VOLUME_MOUNTS_TAGS}},
        }
    }

    python_origin = reconstructable(bar_with_tags_in_job_and_op).get_python_origin()

    python_origin_with_container_context = python_origin._replace(
        repository_origin=python_origin.repository_origin._replace(
            container_context=container_context_config
        )
    )

    # Verifies that raw k8s config for step pods is pulled from the container context and
    # dagster-k8s/config tags on the op, but *not* from tags on the job
    executor = _get_executor(
        k8s_run_launcher_instance,
        reconstructable(bar_with_tags_in_job_and_op),
        {},
    )

    run = create_run_for_test(
        k8s_run_launcher_instance,
        job_name="bar_with_tags_in_job_and_op",
        job_code_origin=python_origin_with_container_context,
    )

    step_handler_context = _step_handler_context(
        pipeline=reconstructable(bar_with_tags_in_job_and_op),
        pipeline_run=run,
        instance=k8s_run_launcher_instance,
        executor=executor,
    )

    container_context = executor._step_handler._get_container_context(  # noqa: SLF001
        step_handler_context
    )

    raw_k8s_config = container_context.get_run_user_defined_k8s_config()

    assert raw_k8s_config.container_config["resources"] == OTHER_RESOURCE_TAGS
    assert raw_k8s_config.container_config["volume_mounts"] == OTHER_VOLUME_MOUNTS_TAGS

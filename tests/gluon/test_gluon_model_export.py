from packaging.version import Version
import os
import warnings
import yaml

import mxnet as mx
import numpy as np
import pandas as pd
import pytest
from mxnet import context as ctx
from mxnet.gluon import Trainer
from mxnet.gluon.contrib.estimator import estimator
from mxnet.gluon.data import DataLoader
from mxnet.gluon.loss import SoftmaxCrossEntropyLoss
from mxnet.gluon.nn import HybridSequential, Dense

import mlflow
import mlflow.gluon
import mlflow.pyfunc.scoring_server as pyfunc_scoring_server
from mlflow import pyfunc
from mlflow.models import infer_signature, Model
from mlflow.models.utils import _read_example
from mlflow.tracking.artifact_utils import _download_artifact_from_uri
from mlflow.utils.environment import _mlflow_conda_env
from mlflow.utils.file_utils import TempDir
from mlflow.utils.model_utils import _get_flavor_configuration

from tests.helper_functions import (
    pyfunc_serve_and_score_model,
    _compare_conda_env_requirements,
    _assert_pip_requirements,
)

if Version(mx.__version__) >= Version("2.0.0"):
    from mxnet.gluon.metric import Accuracy  # pylint: disable=import-error

    array_module = mx.np
else:
    from mxnet.metric import Accuracy  # pylint: disable=import-error

    array_module = mx.nd


@pytest.fixture
def model_path(tmpdir):
    return os.path.join(tmpdir.strpath, "model")


@pytest.fixture
def gluon_custom_env(tmpdir):
    conda_env = os.path.join(str(tmpdir), "conda_env.yml")
    _mlflow_conda_env(conda_env, additional_pip_deps=["mxnet", "pytest"])
    return conda_env


@pytest.fixture(scope="module")
def model_data():
    mnist = mx.test_utils.get_mnist()
    train_data = array_module.array(mnist["train_data"].reshape(-1, 784))
    train_label = array_module.array(mnist["train_label"])
    test_data = array_module.array(mnist["test_data"].reshape(-1, 784))
    return train_data, train_label, test_data


@pytest.fixture(scope="module")
def gluon_model(model_data):
    train_data, train_label, _ = model_data
    dataset = mx.gluon.data.ArrayDataset(train_data, train_label)
    train_data_loader = DataLoader(dataset, batch_size=128, last_batch="discard")
    model = HybridSequential()
    model.add(Dense(128, activation="relu"))
    model.add(Dense(64, activation="relu"))
    model.add(Dense(10))
    model.initialize()
    model.hybridize()
    trainer = Trainer(
        model.collect_params(), "adam", optimizer_params={"learning_rate": 0.001, "epsilon": 1e-07}
    )

    # `metrics` was renamed in mxnet 1.6.0: https://github.com/apache/incubator-mxnet/pull/17048
    arg_name = "metrics" if Version(mx.__version__) < Version("1.6.0") else "train_metrics"
    est = estimator.Estimator(
        net=model, loss=SoftmaxCrossEntropyLoss(), trainer=trainer, **{arg_name: Accuracy()}
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        est.fit(train_data_loader, epochs=3)
    return model


@pytest.mark.large
def test_model_save_load(gluon_model, model_data, model_path):
    _, _, test_data = model_data
    expected = array_module.argmax(gluon_model(test_data), axis=1)

    mlflow.gluon.save_model(gluon_model, model_path)
    # Loading Gluon model
    model_loaded = mlflow.gluon.load_model(model_path, ctx.cpu())
    actual = array_module.argmax(model_loaded(test_data), axis=1)
    assert all(expected == actual)
    # Loading pyfunc model
    pyfunc_loaded = mlflow.pyfunc.load_model(model_path)
    test_pyfunc_data = pd.DataFrame(test_data.asnumpy())
    pyfunc_preds = pyfunc_loaded.predict(test_pyfunc_data)
    assert all(np.argmax(pyfunc_preds.values, axis=1) == expected.asnumpy())
    # test with numpy array input
    pyfunc_preds = pyfunc_loaded.predict(test_pyfunc_data.values)
    assert all(np.argmax(pyfunc_preds, axis=1) == expected.asnumpy())


@pytest.mark.large
def test_signature_and_examples_are_saved_correctly(gluon_model, model_data):
    model = gluon_model
    signature_ = infer_signature(model_data[0].asnumpy())
    example_ = model_data[0].asnumpy()[
        :3,
    ]
    for signature in (None, signature_):
        for example in (None, example_):
            with TempDir() as tmp:
                path = tmp.path("model")
                mlflow.gluon.save_model(
                    model, path=path, signature=signature, input_example=example
                )
                mlflow_model = Model.load(path)
                assert signature == mlflow_model.signature
                if example is None:
                    assert mlflow_model.saved_input_example_info is None
                else:
                    assert np.array_equal(_read_example(mlflow_model, path), example)


@pytest.mark.large
def test_model_log_load(gluon_model, model_data, model_path):
    # pylint: disable=unused-argument
    _, _, test_data = model_data
    expected = array_module.argmax(gluon_model(test_data), axis=1)

    artifact_path = "model"
    with mlflow.start_run():
        mlflow.gluon.log_model(gluon_model, artifact_path=artifact_path)
        model_uri = "runs:/{run_id}/{artifact_path}".format(
            run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
        )

    # Loading Gluon model
    model_loaded = mlflow.gluon.load_model(model_uri, ctx.cpu())
    actual = array_module.argmax(model_loaded(test_data), axis=1)
    assert all(expected == actual)
    # Loading pyfunc model
    pyfunc_loaded = mlflow.pyfunc.load_model(model_uri)
    test_pyfunc_data = pd.DataFrame(test_data.asnumpy())
    pyfunc_preds = pyfunc_loaded.predict(test_pyfunc_data)
    assert all(np.argmax(pyfunc_preds.values, axis=1) == expected.asnumpy())


@pytest.mark.large
def test_model_save_persists_specified_conda_env_in_mlflow_model_directory(
    gluon_model, model_path, gluon_custom_env
):
    mlflow.gluon.save_model(gluon_model=gluon_model, path=model_path, conda_env=gluon_custom_env)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)
    assert saved_conda_env_path != gluon_custom_env

    with open(gluon_custom_env, "r") as f:
        gluon_custom_env_parsed = yaml.safe_load(f)
    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == gluon_custom_env_parsed


@pytest.mark.large
def test_model_save_persists_requirements_in_mlflow_model_directory(
    gluon_model, model_path, gluon_custom_env
):
    mlflow.gluon.save_model(gluon_model=gluon_model, path=model_path, conda_env=gluon_custom_env)

    saved_pip_req_path = os.path.join(model_path, "requirements.txt")
    _compare_conda_env_requirements(gluon_custom_env, saved_pip_req_path)


@pytest.mark.large
def test_save_model_with_pip_requirements(gluon_model, tmpdir):
    # Path to a requirements file
    tmpdir1 = tmpdir.join("1")
    req_file = tmpdir.join("requirements.txt")
    req_file.write("a")
    mlflow.gluon.save_model(gluon_model, tmpdir1.strpath, pip_requirements=req_file.strpath)
    _assert_pip_requirements(tmpdir1.strpath, ["mlflow", "a"])

    # List of requirements
    tmpdir2 = tmpdir.join("2")
    mlflow.gluon.save_model(
        gluon_model, tmpdir2.strpath, pip_requirements=[f"-r {req_file.strpath}", "b"]
    )
    _assert_pip_requirements(tmpdir2.strpath, ["mlflow", "a", "b"])

    # Constraints file
    tmpdir3 = tmpdir.join("3")
    mlflow.gluon.save_model(
        gluon_model, tmpdir3.strpath, pip_requirements=[f"-c {req_file.strpath}", "b"]
    )
    _assert_pip_requirements(tmpdir3.strpath, ["mlflow", "b", "-c constraints.txt"], ["a"])


@pytest.mark.large
def test_save_model_with_extra_pip_requirements(gluon_model, tmpdir):
    default_reqs = mlflow.gluon.get_default_pip_requirements()

    # Path to a requirements file
    tmpdir1 = tmpdir.join("1")
    req_file = tmpdir.join("requirements.txt")
    req_file.write("a")
    mlflow.gluon.save_model(gluon_model, tmpdir1.strpath, extra_pip_requirements=req_file.strpath)
    _assert_pip_requirements(tmpdir1.strpath, ["mlflow", *default_reqs, "a"])

    # List of requirements
    tmpdir2 = tmpdir.join("2")
    mlflow.gluon.save_model(
        gluon_model, tmpdir2.strpath, extra_pip_requirements=[f"-r {req_file.strpath}", "b"]
    )
    _assert_pip_requirements(tmpdir2.strpath, ["mlflow", *default_reqs, "a", "b"])

    # Constraints file
    tmpdir3 = tmpdir.join("3")
    mlflow.gluon.save_model(
        gluon_model, tmpdir3.strpath, extra_pip_requirements=[f"-c {req_file.strpath}", "b"]
    )
    _assert_pip_requirements(
        tmpdir3.strpath, ["mlflow", *default_reqs, "b", "-c constraints.txt"], ["a"]
    )


@pytest.mark.large
def test_model_save_accepts_conda_env_as_dict(gluon_model, model_path):
    conda_env = dict(mlflow.gluon.get_default_conda_env())
    conda_env["dependencies"].append("pytest")
    mlflow.gluon.save_model(gluon_model=gluon_model, path=model_path, conda_env=conda_env)

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)

    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == conda_env


@pytest.mark.large
def test_log_model_persists_specified_conda_env_in_mlflow_model_directory(
    gluon_model, gluon_custom_env
):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.gluon.log_model(
            gluon_model=gluon_model, artifact_path=artifact_path, conda_env=gluon_custom_env
        )
        model_path = _download_artifact_from_uri(
            "runs:/{run_id}/{artifact_path}".format(
                run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
            )
        )

    pyfunc_conf = _get_flavor_configuration(model_path=model_path, flavor_name=pyfunc.FLAVOR_NAME)
    saved_conda_env_path = os.path.join(model_path, pyfunc_conf[pyfunc.ENV])
    assert os.path.exists(saved_conda_env_path)
    assert saved_conda_env_path != gluon_custom_env

    with open(gluon_custom_env, "r") as f:
        gluon_custom_env_parsed = yaml.safe_load(f)
    with open(saved_conda_env_path, "r") as f:
        saved_conda_env_parsed = yaml.safe_load(f)
    assert saved_conda_env_parsed == gluon_custom_env_parsed


@pytest.mark.large
def test_model_log_persists_requirements_in_mlflow_model_directory(gluon_model, gluon_custom_env):
    artifact_path = "model"
    with mlflow.start_run():
        mlflow.gluon.log_model(
            gluon_model=gluon_model, artifact_path=artifact_path, conda_env=gluon_custom_env
        )
        model_path = _download_artifact_from_uri(
            "runs:/{run_id}/{artifact_path}".format(
                run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
            )
        )

    saved_pip_req_path = os.path.join(model_path, "requirements.txt")
    _compare_conda_env_requirements(gluon_custom_env, saved_pip_req_path)


@pytest.mark.large
def test_gluon_model_serving_and_scoring_as_pyfunc(gluon_model, model_data):
    _, _, test_data = model_data
    expected = array_module.argmax(gluon_model(test_data), axis=1)

    artifact_path = "model"
    with mlflow.start_run():
        mlflow.gluon.log_model(gluon_model, artifact_path=artifact_path)
        model_uri = "runs:/{run_id}/{artifact_path}".format(
            run_id=mlflow.active_run().info.run_id, artifact_path=artifact_path
        )

    scoring_response = pyfunc_serve_and_score_model(
        model_uri=model_uri,
        data=pd.DataFrame(test_data.asnumpy()),
        content_type=pyfunc_scoring_server.CONTENT_TYPE_JSON_SPLIT_ORIENTED,
        extra_args=["--no-conda"],
    )
    response_values = pd.read_json(scoring_response.content, orient="records").values.astype(
        np.float32
    )
    assert all(np.argmax(response_values, axis=1) == expected.asnumpy())

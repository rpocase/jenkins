import asyncio
import pytest
import random
import json
import os
from .logger import log
from subprocess import check_output
from shlex import split


def get_test_arn():
    env = os.environ.get("TEST_ARN")
    if not env:
        # default to garbage
        env = "arn:aws:iam::xxxxxxxx:role/k8s-view-role"
        log("Warning: Using bogus arn!")
    return env


def get_test_keys():
    key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not key_id:
        log("Warning: Invalid key ID being used")
        key_id = "DEADBEEFDEADBEEF"
    if not key:
        log("Warning: Invalid key being used")
        key = "INVALIDKEYINVALIDKEYINVALIDKEY"
    return {'id': key_id, 'key': key}


async def run_auth(one_master, args):
    creds = get_test_keys()
    cmd = "AWS_ACCESS_KEY_ID={} AWS_SECRET_ACCESS_KEY={} \
           /snap/bin/kubectl --context=aws-iam-authenticator \
           --kubeconfig /home/ubuntu/aws-kubeconfig \
           {}".format(creds['id'], creds['key'], args)
    output = await one_master.run(cmd, timeout=15)
    assert output.status == "completed"
    return output.data["results"]["Stderr"].lower()


async def verify_auth_success(one_master, args):
    error_text = run_auth(one_master, args)
    assert "invalid user credentials" not in error_text
    assert "error" not in error_text
    assert "forbidden" not in error_text


async def verify_auth_failure(one_master, args):
    error_text = run_auth(one_master, args)
    assert "invalid user credentials" in error_text or \
           "error" in error_text or \
           "forbidden" in error_text


async def patch_kubeconfig_and_verify_aws_iam(one_master):
    log("patching and validating generated kubectl config file")
    for i in range(6):
        output = await one_master.run("cat /home/ubuntu/config")
        if 'aws-iam-user' in output.results['Stdout']:
            await one_master.run("cp /home/ubuntu/config "
                                 "/home/ubuntu/aws-kubeconfig")
            cmd = "sed -i 's;<<insert_arn_here>>;{};'" \
                " /home/ubuntu/aws-kubeconfig".format(get_test_arn())
            await one_master.run(cmd)
            break
        log("Unable to find AWS IAM information in kubeconfig, retrying...")
        await asyncio.sleep(10)
    assert 'aws-iam-user' in output.results['Stdout']


@pytest.mark.asyncio
async def test_validate_aws_iam(model, tools):
    # This test verifies the aws-iam charm is working
    # properly. This requires:
    # 1) Deploy aws-iam and relate
    # 2) Deploy CRD
    # 3) Grab new kubeconfig from master.
    # 4) Plug in test ARN to config
    # 5) Download aws-iam-authenticator binary
    # 6) Verify authentication via aws user
    # 7) Turn on RBAC
    # 8) Verify unauthorized access
    # 9) Grant access via RBAC
    # 10) Verify access

    log("starting aws-iam test")
    masters = model.applications["kubernetes-master"]
    k8s_version_str = masters.data["workload-version"]
    k8s_minor_version = tuple(int(i) for i in k8s_version_str.split(".")[:2])
    if k8s_minor_version < (1, 15):
        log("skipping, k8s version v" + k8s_version_str)
        return

    # 1) deploy
    log("deploying aws-iam")
    await model.deploy("cs:~containers/aws-iam", num_units=0)
    await model.add_relation("aws-iam", "kubernetes-master")
    await model.add_relation("aws-iam", "easyrsa")
    log("waiting for cluster to settle...")
    await tools.juju_wait()

    # 2) deploy CRD for test
    log("deploying crd")
    cmd = """/snap/bin/kubectl apply -f - << EOF
apiVersion: iamauthenticator.k8s.aws/v1alpha1
kind: IAMIdentityMapping
metadata:
  name: kubernetes-admin
spec:
  arn: {}
  username: test-user
  groups:
  - view
EOF""".format(get_test_arn())
    # Note that we patch a single master's kubeconfig to have the arn in it,
    # so we need to use that one master for all commands
    one_master = random.choice(masters.units)
    output = await one_master.run(cmd, timeout=15)
    assert output.status == "completed"

    # 3 & 4) grab config and verify aws-iam is inside
    log("verifying kubeconfig")
    await patch_kubeconfig_and_verify_aws_iam(one_master)

    # 5) get aws-iam-authenticator binary
    log("getting aws-iam binary")
    cmd = "curl -s https://api.github.com/repos/kubernetes-sigs/aws-iam-authenticator/releases/latest"
    data = json.loads(check_output(split(cmd)).decode('utf-8'))
    for asset in data['assets']:
        if 'linux_amd64' in asset['browser_download_url']:
            latest_release_url = asset['browser_download_url']
            break

    auth_bin = "/usr/local/bin/aws-iam-authenticator"
    cmd = "wget -q -nv -O {} {}"
    output = await one_master.run(cmd.format(auth_bin, latest_release_url),
                                  timeout=15)
    assert output.status == "completed"

    output = await one_master.run("chmod a+x {}".format(auth_bin), timeout=15)
    assert output.status == "completed"

    # 6) Auth as a user - note that creds come in the environment as a
    #    jenkins secret
    await verify_auth_success(one_master, "get po")

    # 7) turn on RBAC and add a test user
    await masters.set_config({"authorization-mode": "RBAC,Node"})
    log("waiting for cluster to settle...")
    await tools.juju_wait()

    # 8) verify failure
    await verify_auth_failure(one_master, "get po")

    # 9) grant user access
    cmd = """/snap/bin/kubectl apply -f - << EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: pod-reader
rules:
- apiGroups: [""]
  resources:
  - pods
  verbs:
  - get
  - list
  - watch

---
apiVersion: rbac.authorization.k8s.io/v1
# This role binding allows "test-user" to read pods in the "default" namespace.
kind: RoleBinding
metadata:
  name: read-pods
  namespace: default
subjects:
- kind: User
  name: test-user
  apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io
EOF"""
    output = await one_master.run(cmd, timeout=15)
    assert output.status == "completed"

    # 10) verify success
    await verify_auth_success(one_master, "get po")

    # 11) verify overstep failure
    await verify_auth_failure(one_master, "get po -n kube-system")

    # teardown
    await masters.set_config({"authorization-mode": "AlwaysAllow"})
    await model.applications["aws-iam"].destroy()
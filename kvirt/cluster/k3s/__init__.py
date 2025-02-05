#!/usr/bin/env python

from kvirt.common import success, pprint, warning, get_kubectl, info2, container_mode, kube_create_app
from kvirt.common import deploy_cloud_storage, wait_cloud_dns
import os
import re
from random import choice
from shutil import which
from string import ascii_letters, digits
from subprocess import call
from tempfile import NamedTemporaryFile
import yaml
virtplatforms = ['kvm', 'kubevirt', 'ovirt', 'openstack', 'vsphere']
cloudplatforms = ['aws', 'gcp']


def scale(config, plandir, cluster, overrides):
    plan = cluster
    data = {'cluster': cluster, 'kube': cluster, 'kubetype': 'k3s', 'image': 'ubuntu2004', 'sdn': 'flannel',
            'extra_scripts': [], 'cloud_lb': True}
    data['basedir'] = '/workdir' if container_mode() else '.'
    cluster = data.get('cluster')
    clusterdir = os.path.expanduser(f"~/.kcli/clusters/{cluster}")
    if os.path.exists(f"{clusterdir}/kcli_parameters.yml"):
        with open(f"{clusterdir}/kcli_parameters.yml", 'r') as install:
            installparam = yaml.safe_load(install)
            data.update(installparam)
            plan = installparam.get('plan', plan)
    data.update(overrides)
    sdn = data['sdn']
    client = config.client
    pprint(f"Scaling on client {client}")
    if os.path.exists(clusterdir):
        with open(f"{clusterdir}/kcli_parameters.yml", 'w') as paramfile:
            yaml.safe_dump(data, paramfile)
    vmrules_all_names = []
    if data.get('vmrules', config.vmrules) and data.get('vmrules_strict', config.vmrules_strict):
        vmrules_all_names = [list(entry.keys())[0] for entry in data.get('vmrules', config.vmrules)]
    for role in ['ctlplanes', 'workers']:
        install_k3s_args = []
        for arg in data:
            if arg.startswith('install_k3s'):
                install_k3s_args.append(f"{arg.upper()}={data[arg]}")
        overrides = data.copy()
        overrides['scale'] = True
        threaded = data.get('threaded', False) or data.get(f'{role}_threaded', False)
        if role == 'ctlplanes':
            if overrides.get('ctlplanes', 1) == 1:
                continue
            if 'virtual_router_id' not in overrides or 'auth_pass' not in overrides:
                warning("Scaling up of ctlplanes won't work without virtual_router_id and auth_pass")
            if sdn is None or sdn != 'flannel':
                install_k3s_args.append("INSTALL_K3S_EXEC='--flannel-backend=none'")
            install_k3s_args = ' '.join(install_k3s_args)
        if role == 'workers' and overrides.get('workers', 0) == 0:
            continue
        if vmrules_all_names:
            reg = re.compile(f'{cluster}-{role[:-1]}-[0-9]+')
            vmrules_names = [name for name in vmrules_all_names if reg.match(name)]
            if len(vmrules_names) != overrides.get(role, 1):
                warning(f"Adjusting {role} number to vmrule entries")
                overrides[role] = len(vmrules_names)
            overrides['vmrules_names'] = sorted(vmrules_names)
        overrides['install_k3s_args'] = install_k3s_args
        result = config.plan(plan, inputfile=f'{plandir}/{role}.yml', overrides=overrides, threaded=threaded)
        if result['result'] != 'success':
            return result
    return {'result': 'success'}


def create(config, plandir, cluster, overrides):
    platform = config.type
    data = {'kubetype': 'k3s', 'ctlplanes': 1, 'workers': 0, 'sdn': 'flannel', 'extra_scripts': [], 'autoscale': False,
            'network': 'default', 'cloud_lb': None}
    data.update(overrides)
    data['cloud_lb'] = overrides.get('cloud_lb', platform in cloudplatforms and data['ctlplanes'] > 1)
    cloud_lb = data['cloud_lb']
    data['cluster'] = overrides.get('cluster', cluster if cluster is not None else 'myk3s')
    plan = cluster if cluster is not None else data['cluster']
    data['kube'] = data['cluster']
    autoscale = data['autoscale']
    ctlplanes = data['ctlplanes']
    workers = data['workers']
    network = data['network']
    sdn = None if 'sdn' in overrides and overrides['sdn'] is None else data.get('sdn')
    domain = data.get('domain', 'karmalabs.corp')
    image = data.get('image', 'ubuntu2004')
    api_ip = data.get('api_ip')
    if ctlplanes > 1:
        if platform in cloudplatforms:
            if not cloud_lb:
                msg = "Multiple ctlplanes require cloud_lb to be set to True"
                return {'result': 'failure', 'reason': msg}
            api_ip = f"api.{cluster}.{domain}"
            data['api_ip'] = api_ip
        elif api_ip is None:
            if network == 'default' and platform == 'kvm':
                warning("Using 192.168.122.253 as api_ip")
                data['api_ip'] = "192.168.122.253"
                api_ip = "192.168.122.253"
            elif platform == 'kubevirt':
                selector = {'kcli/plan': plan, 'kcli/role': 'ctlplane'}
                api_ip = config.k.create_service(f"{cluster}-api", config.k.namespace, selector,
                                                 _type="LoadBalancer", ports=[6443])
                if api_ip is None:
                    msg = "Couldnt get an kubevirt api_ip from service"
                    return {'result': 'failure', 'reason': msg}
                else:
                    pprint(f"Using api_ip {api_ip}")
                    data['api_ip'] = api_ip
            else:
                msg = "You need to define api_ip in your parameters file"
                return {'result': 'failure', 'reason': msg}
        if not cloud_lb and ctlplanes > 1 and data.get('virtual_router_id') is None:
            data['virtual_router_id'] = hash(data['cluster']) % 254 + 1
            pprint(f"Using keepalived virtual_router_id {data['virtual_router_id']}")
    virtual_router_id = data.get('virtual_router_id')
    if data.get('auth_pass') is None:
        auth_pass = ''.join(choice(ascii_letters + digits) for i in range(5))
        data['auth_pass'] = auth_pass
    install_k3s_args = []
    for arg in data:
        if arg.startswith('install_k3s'):
            install_k3s_args.append(f"{arg.upper()}={data[arg]}")
    cluster = data.get('cluster')
    clusterdir = os.path.expanduser(f"~/.kcli/clusters/{cluster}")
    if os.path.exists(clusterdir):
        msg = f"Remove existing directory {clusterdir} or use --force"
        return {'result': 'failure', 'reason': msg}
    if which('kubectl') is None:
        get_kubectl()
    if not os.path.exists(clusterdir):
        os.makedirs(clusterdir)
        os.mkdir(f"{clusterdir}/auth")
        with open(f"{clusterdir}/kcli_parameters.yml", 'w') as p:
            installparam = overrides.copy()
            installparam['api_ip'] = api_ip
            installparam['plan'] = plan
            installparam['kubetype'] = 'k3s'
            installparam['image'] = image
            installparam['auth_pass'] = auth_pass
            installparam['virtual_router_id'] = virtual_router_id
            installparam['cluster'] = cluster
            yaml.safe_dump(installparam, p, default_flow_style=False, encoding='utf-8', allow_unicode=True)
    for arg in data.get('extra_ctlplane_args', []):
        if arg.startswith('--data-dir='):
            data['data_dir'] = arg.split('=')[1]
    bootstrap_overrides = data.copy()
    if os.path.exists("manifests") and os.path.isdir("manifests"):
        bootstrap_overrides['files'] = [{"path": "/root/manifests", "currentdir": True, "origin": "manifests"}]
    bootstrap_install_k3s_args = install_k3s_args.copy()
    if sdn is None or sdn != 'flannel':
        bootstrap_install_k3s_args.append("INSTALL_K3S_EXEC='--flannel-backend=none'")
    bootstrap_install_k3s_args = ' '.join(bootstrap_install_k3s_args)
    bootstrap_overrides['install_k3s_args'] = bootstrap_install_k3s_args
    result = config.plan(plan, inputfile=f'{plandir}/bootstrap.yml', overrides=bootstrap_overrides)
    if result['result'] != "success":
        return result
    for role in ['ctlplanes', 'workers']:
        if (role == 'ctlplanes' and ctlplanes == 1) or (role == 'workers' and workers == 0):
            continue
        nodes_overrides = data.copy()
        nodes_install_k3s_args = install_k3s_args.copy()
        nodes_overrides['install_k3s_args'] = nodes_install_k3s_args
        if role == 'ctlplanes':
            if sdn is None or sdn != 'flannel':
                nodes_install_k3s_args.append("INSTALL_K3S_EXEC='--flannel-backend=none'")
            nodes_install_k3s_args = ' '.join(nodes_install_k3s_args)
            nodes_overrides['install_k3s_args'] = nodes_install_k3s_args
            pprint("Deploying extra ctlplanes")
            threaded = data.get('threaded', False) or data.get('ctlplanes_threaded', False)
            config.plan(plan, inputfile=f'{plandir}/ctlplanes.yml', overrides=nodes_overrides, threaded=threaded)
        if role == 'workers':
            pprint("Deploying workers")
            os.chdir(os.path.expanduser("~/.kcli"))
            threaded = data.get('threaded', False) or data.get('workers_threaded', False)
            config.plan(plan, inputfile=f'{plandir}/workers.yml', overrides=nodes_overrides, threaded=threaded)
    if cloud_lb and config.type in cloudplatforms:
        config.k.delete_dns(f'api.{cluster}', domain=domain)
        if config.type == 'aws':
            data['vpcid'] = config.k.get_vpcid_of_vm(f"{cluster}-ctlplane-0")
        result = config.plan(plan, inputfile=f'{plandir}/cloud_lb_api.yml', overrides=data)
        if result['result'] != 'success':
            return result
    success(f"K3s cluster {cluster} deployed!!!")
    info2(f"export KUBECONFIG=$HOME/.kcli/clusters/{cluster}/auth/kubeconfig")
    info2("export PATH=$PWD:$PATH")
    if config.type in cloudplatforms and cloud_lb:
        wait_cloud_dns(cluster, domain)
    os.environ['KUBECONFIG'] = f"{clusterdir}/auth/kubeconfig"
    apps = data.get('apps', [])
    if apps:
        appdir = f"{plandir}/apps"
        os.environ["PATH"] = f'{os.getcwd()}:{os.environ["PATH"]}'
        for app in apps:
            app_data = data.copy()
            if not os.path.exists(appdir):
                warning(f"Skipping unsupported app {app}")
            else:
                pprint(f"Adding app {app}")
                if f'{app}_version' not in overrides:
                    app_data[f'{app}_version'] = 'latest'
                kube_create_app(config, app, appdir, overrides=app_data)
    if autoscale:
        config.import_in_kube(network=network, secure=True)
        with NamedTemporaryFile(mode='w+t') as temp:
            commondir = os.path.dirname(pprint.__code__.co_filename)
            autoscale_overrides = {'cluster': cluster, 'kubetype': 'k3s', 'workers': workers, 'replicas': 1}
            autoscale_data = config.process_inputfile(cluster, f"{commondir}/autoscale.yaml.j2",
                                                      overrides=autoscale_overrides)
            temp.write(autoscale_data)
            autoscalecmd = f"kubectl create -f {temp.name}"
            call(autoscalecmd, shell=True)
    if config.type in cloudplatforms and data.get('cloud_storage', True):
        pprint("Deploying cloud storage class")
        apply = config.type == 'aws'
        deploy_cloud_storage(config, cluster, apply=apply)
    return {'result': 'success'}

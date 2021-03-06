"""
Task executor
"""
import time
import logging
import json
from uuid import uuid4
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
import schedule
from django.db.models import Q
import rpyc
from rpyc.utils.server import ThreadedServer
from kubernetes import client
from kubernetes.stream import stream
from kubernetes.client import CoreV1Api, BatchV1Api, ExtensionsV1beta1Api, AppsV1Api
from kubernetes.client.rest import ApiException
from api.common import get_kubernetes_api_client, USERSPACE_NAME, random_password, get_uuid
from config import DAEMON_WORKERS, KUBERNETES_NAMESPACE, CEPH_STORAGE_CLASS_NAME, \
    GLOBAL_TASK_TIME_LIMIT, USER_SPACE_POD_TIMEOUT, IPC_PORT, USER_WEBSHELL_DOCKER_IMAGE
import config
from user_model.models import UserModel
from .models import TaskSettings, TaskStorage, TaskVNCPod, Task, TASK

LOGGER = logging.getLogger(__name__)


def create_namespace():
    api_instance = CoreV1Api(get_kubernetes_api_client())
    try:
        api_instance.create_namespace(client.V1Namespace(api_version="v1", kind="Namespace",
                                                         metadata=
                                                         client.V1ObjectMeta(name=KUBERNETES_NAMESPACE,
                                                                             labels={
                                                                                 "name": KUBERNETES_NAMESPACE})))
    except ApiException:
        # namespaces already exists
        pass


def create_userspace_pvc():
    api_instance = CoreV1Api(get_kubernetes_api_client())
    userspace = client.V1PersistentVolumeClaim(api_version="v1", kind="PersistentVolumeClaim",
                                               metadata=client.V1ObjectMeta(name=USERSPACE_NAME,
                                                                            namespace=KUBERNETES_NAMESPACE),
                                               spec=client.V1PersistentVolumeClaimSpec(
                                                   access_modes=["ReadWriteMany"],
                                                   resources=
                                                   client.V1ResourceRequirements(
                                                       requests=
                                                       {"storage": "1024Gi"}),
                                                   storage_class_name=CEPH_STORAGE_CLASS_NAME))
    try:
        api_instance.create_namespaced_persistent_volume_claim(KUBERNETES_NAMESPACE, userspace)
    except ApiException:
        pass


def get_userspace_pvc():
    api_instance = CoreV1Api(get_kubernetes_api_client())
    try:
        _ = api_instance.read_namespaced_persistent_volume_claim(namespace=KUBERNETES_NAMESPACE, name=USERSPACE_NAME)
        return True
    except ApiException:
        return False


def get_short_uuid():
    return str(uuid4())[:8]


def config_checker(json_config):
    try:
        pre_check_fail = ('image' not in json_config.keys() or
                          'persistent_volume' not in json_config.keys() or
                          'name' not in json_config['persistent_volume'].keys() or
                          'mount_path' not in json_config['persistent_volume'].keys() or
                          'working_path' not in json_config.keys() or
                          'shell' not in json_config.keys() or
                          'memory_limit' not in json_config.keys() or
                          'commands' not in json_config.keys() or
                          'task_script_path' not in json_config.keys() or
                          'task_initial_file_path' not in json_config.keys() or
                          not isinstance(json_config['commands'], list))
        return not pre_check_fail
    except Exception as _:
        return False


class Singleton:
    """
    A non-thread-safe helper class to ease implementing singletons.
    This should be used as a decorator -- not a metaclass -- to the
    class that should be a singleton.

    The decorated class can define one `__init__` function that
    takes only the `self` argument. Also, the decorated class cannot be
    inherited from. Other than that, there are no restrictions that apply
    to the decorated class.

    To get the singleton instance, use the `instance` method. Trying
    to use `__call__` will result in a `TypeError` being raised.

    """

    def __init__(self, decorated):
        self._decorated = decorated
        self._instance = None

    def instance(self, new=True, **kwargs):
        """
        Returns the singleton instance. Upon its first call, it creates a
        new instance of the decorated class and calls its `__init__` method.
        On all subsequent calls, the already created instance is returned.

        """
        if new and self._instance is None:
            self._instance = self._decorated(**kwargs)
        return self._instance

    def __call__(self):
        raise TypeError('Singletons must be accessed through `instance()`.')

    def __instancecheck__(self, inst):
        return isinstance(inst, self._decorated)


class RpcService(rpyc.Service):
    def exposed_get_user_space_pod(self, uuid, user_id):
        executor = TaskExecutor.instance(new=False)
        try:
            user = UserModel.objects.get(uuid=user_id)
        except UserModel.DoesNotExist:
            return None
        if executor:
            ret = executor.get_user_space_pod(uuid, user)
            if ret and hasattr(ret, 'metadata') and hasattr(ret.metadata, 'name'):
                return ret.metadata.name
            else:
                return None
        else:
            return None


@Singleton
class TaskExecutor:
    def __init__(self, test=False):
        self.ttl_checker = ThreadPoolExecutor(max_workers=DAEMON_WORKERS,
                                              thread_name_prefix='cloud_scheduler_k8s_worker_ttl')
        self.scheduler_thread = Thread(target=self.dispatch)
        self.job_dispatch_thread = Thread(target=self._job_dispatch)
        self.job_monitor_thread = Thread(target=self._job_monitor)
        self.storage_pod_monitor_thread = Thread(target=self._storage_pod_monitor)
        self.ready = False
        self.test = test
        self.ipc_server = ThreadedServer(RpcService, port=IPC_PORT)
        self.ipc_thread = Thread(target=self.ipc_server.start)
        LOGGER.info("Task executor initialized.")

    def start(self):
        if not self.scheduler_thread.isAlive():
            self.scheduler_thread.start()
            LOGGER.info("Task executor started.")
        else:
            LOGGER.info("Task executor already started")
        if not self.job_dispatch_thread.isAlive():
            self.job_dispatch_thread.start()
        if not self.job_monitor_thread.isAlive():
            self.job_monitor_thread.start()
        if not self.storage_pod_monitor_thread.isAlive():
            self.storage_pod_monitor_thread.start()
        if not self.ipc_thread.isAlive():
            self.ipc_thread.start()

    def _run_job(self, fn, **kwargs):
        self.ttl_checker.submit(fn, **kwargs)

    def _storage_pod_monitor(self):
        api = CoreV1Api(get_kubernetes_api_client())
        app_api = AppsV1Api(get_kubernetes_api_client())

        def _actual_work():
            idle = True
            try:
                for item in TaskStorage.objects.filter(expire_time__gt=0).order_by('expire_time'):
                    try:
                        if item.expire_time <= round(time.time()):
                            # release idled pod
                            idle = False
                            username = '{}_{}'.format(item.user.username, item.settings.id)
                            pod = api.read_namespaced_pod(name=item.pod_name, namespace=KUBERNETES_NAMESPACE)
                            if pod is not None and pod.status is not None and pod.status.phase == 'Running':
                                response = stream(api.connect_get_namespaced_pod_exec,
                                                  item.pod_name,
                                                  KUBERNETES_NAMESPACE,
                                                  command=
                                                  ['/bin/bash', '-c',
                                                   'unlink /home/{username};userdel {username}'.format(
                                                       username=username)],
                                                  stderr=True, stdin=False,
                                                  stdout=True, tty=False)
                                LOGGER.debug(response)
                                pod.metadata.labels['occupied'] = str(max(int(pod.metadata.labels['occupied']) - 1, 0))
                                api.patch_namespaced_pod(pod.metadata.name, KUBERNETES_NAMESPACE, pod)
                            item.pod_name = ''
                            item.expire_time = 0
                            item.save(force_update=True)
                    except ApiException as ex:
                        if ex.status == 404:
                            item.pod_name = ''
                            item.expire_time = 0
                            item.save(force_update=True)
                        else:
                            LOGGER.warning(ex)
                for item in TaskVNCPod.objects.filter(expire_time__gt=0).order_by('expire_time'):
                    try:
                        if item.expire_time <= round(time.time()):
                            idle = False
                            if item.pod_name:
                                app_api.delete_namespaced_deployment(name=item.pod_name, namespace=KUBERNETES_NAMESPACE)
                                item.pod_name = ''
                                item.expire_time = 0
                                item.url_path = ''
                                item.save(force_update=True)
                    except ApiException as ex:
                        if ex.status != 404:
                            LOGGER.exception(ex)
                        else:
                            item.pod_name = ''
                            item.expire_time = 0
                            item.url_path = ''
                            item.save(force_update=True)
            except Exception as ex:
                LOGGER.warning(ex)
            if idle:
                time.sleep(1)

        while True:
            _actual_work()
            if self.test:
                break

    def _job_monitor(self):
        api = CoreV1Api(get_kubernetes_api_client())
        job_api = BatchV1Api(get_kubernetes_api_client())

        def _actual_work():
            idle = True
            try:
                for item in Task.objects.filter(Q(status=TASK.WAITING) | Q(status=TASK.RUNNING) |
                                                Q(status=TASK.PENDING)).order_by("create_time"):
                    common_name = "task-exec-{}".format(item.uuid)
                    try:
                        response = api.list_namespaced_pod(namespace=KUBERNETES_NAMESPACE,
                                                           label_selector="task-exec={}".format(item.uuid))
                        if response.items:
                            status = response.items[0].status.phase
                            new_status = item.status
                            deleting = response.items[0].metadata.deletion_timestamp
                            if status == 'Running':
                                new_status = TASK.RUNNING
                            elif status == 'Succeeded':
                                new_status = TASK.SUCCEEDED
                            elif status == 'Pending' and not deleting:
                                new_status = TASK.PENDING
                            elif status == 'Failed':
                                new_status = TASK.FAILED
                            if new_status != item.status:
                                if status in ('Succeeded', 'Failed'):
                                    exit_code = None
                                    detailed_status = response.items[0].status.container_statuses
                                    if detailed_status and detailed_status[0].state.terminated:
                                        exit_code = detailed_status[0].state.terminated.exit_code
                                        LOGGER.debug(exit_code)
                                    response = api.read_namespaced_pod_log(name=response.items[0].metadata.name,
                                                                           namespace=KUBERNETES_NAMESPACE)
                                    if response:
                                        item.logs = response
                                    item.logs_get = True
                                    if exit_code:
                                        item.exit_code = exit_code
                                    if exit_code == 124:  # SIGTERM by TLE
                                        item.logs += "\nTime limit exceeded when executing job."
                                        new_status = TASK.TLE
                                    elif exit_code == 137:  # SIGKILL by MLE
                                        item.logs += "\nMemory limit exceeded when executing job."
                                        new_status = TASK.MLE
                                    job_api.delete_namespaced_job(name=common_name,
                                                                  namespace=KUBERNETES_NAMESPACE,
                                                                  body=client.V1DeleteOptions(
                                                                      propagation_policy='Foreground',
                                                                      grace_period_seconds=3
                                                                  ))
                                item.status = new_status
                                idle = False
                                item.save(force_update=True)
                        # else wait for a period because it takes time for corresponding pod to be initialized
                    except ApiException as ex:
                        LOGGER.warning(ex)
                for item in Task.objects.filter(status=TASK.DELETING):
                    common_name = "task-exec-{}".format(item.uuid)
                    try:
                        _ = job_api.delete_namespaced_job(name=common_name,
                                                          namespace=KUBERNETES_NAMESPACE,
                                                          body=client.V1DeleteOptions(
                                                              propagation_policy='Foreground',
                                                              grace_period_seconds=5
                                                          ))
                        LOGGER.info("The kubernetes job of Task: %s deleted successfully", item.uuid)
                        item.delete()
                    except ApiException as ex:
                        if ex.status == 404:
                            item.delete()
                        else:
                            LOGGER.warning("Kubernetes ApiException %d: %s", ex.status, ex.reason)
                    except Exception as ex:
                        LOGGER.error(ex)

            except Exception as ex:
                LOGGER.error(ex)
            if idle:
                time.sleep(1)

        while True:
            _actual_work()
            if self.test:
                break

    @staticmethod
    def get_user_vnc_pod(uuid, user):
        extension_api = ExtensionsV1beta1Api(get_kubernetes_api_client())
        app_api = AppsV1Api(get_kubernetes_api_client())
        core_api = CoreV1Api(get_kubernetes_api_client())
        result = {}
        has_deployment = False
        user_vnc = None
        try:
            setting = TaskSettings.objects.get(uuid=uuid)
            user_vnc, _ = TaskVNCPod.objects.get_or_create(settings=setting, user=user, defaults={
                'settings': setting,
                'user': user,
                'pod_name': '',
                'url_path': '',
                'vnc_password': '',
                'expire_time': round(time.time() + USER_SPACE_POD_TIMEOUT)
            })
            _, created = TaskStorage.objects.get_or_create(settings=setting, user=user, defaults={
                'settings': setting,
                'user': user,
                'pod_name': ''
            })
            if user_vnc.pod_name:
                try:
                    # check whether deployment is on
                    has_deployment = True
                    _ = app_api.read_namespaced_deployment(name=user_vnc.pod_name, namespace=KUBERNETES_NAMESPACE)
                except ApiException as ex:
                    if ex.status != 404:
                        LOGGER.exception(ex)
                    else:
                        has_deployment = False
            selector = "task-{}-user-{}-vnc".format(setting.uuid, user.id)
            if not has_deployment:
                # create a new deployment
                conf = json.loads(setting.container_config)
                user_dir = "user_{}_task_{}".format(user.id, setting.id)
                dep_name = "task-vnc-{}-{}".format(setting.uuid, get_short_uuid())
                shared_pvc_name = "shared-{}".format(setting.uuid)
                shared_mount = client.V1VolumeMount(mount_path=conf['persistent_volume']['mount_path'],
                                                    name=shared_pvc_name, read_only=True)
                user_storage_name = "user-{}".format(setting.uuid)
                user_mount = client.V1VolumeMount(mount_path='/cloud_scheduler_userspace', name=user_storage_name,
                                                  sub_path=user_dir)
                username = '{}_{}'.format(user.username, setting.id)

                commands = ['set +e',
                            'ln -s /cloud_scheduler_userspace /headless/Desktop/user_space',
                            'useradd -u {uid} {username}'.format(uid=499 + user.id, username=username),
                            'usermod -d /headless {}'.format(username),
                            "su -s /bin/bash -c '/dockerstartup/vnc_startup.sh -w' {}".format(username)]
                if created:
                    cp_command = 'cp -r {}/* /headless/Desktop/user_space'.format(
                        conf['persistent_volume']['mount_path'] + '/' + conf['task_initial_file_path'])
                    chown = 'chown -R {user}:{user} /headless/Desktop/user_space/*'.format(user=username)
                    commands.insert(4, cp_command)
                    commands.insert(5, chown)
                vnc_pw = random_password(8)
                env_vnc_pw = client.V1EnvVar(name="VNC_PW", value=vnc_pw)
                container = client.V1Container(
                    name='headless-vnc',
                    image=config.USER_VNC_DOCKER_IMAGE,
                    env=[env_vnc_pw],
                    command=['/bin/bash'],
                    args=['-c', ';'.join(commands)],
                    volume_mounts=[shared_mount, user_mount]
                )
                persistent_volume_claim = client.V1PersistentVolumeClaimVolumeSource(claim_name=
                                                                                     conf['persistent_volume']['name'])
                user_volume_claim = client.V1PersistentVolumeClaimVolumeSource(claim_name=USERSPACE_NAME)
                shared_volume = client.V1Volume(name=shared_pvc_name,
                                                persistent_volume_claim=persistent_volume_claim)
                user_volume = client.V1Volume(name=user_storage_name,
                                              persistent_volume_claim=user_volume_claim)
                template = client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={'app': selector}),
                    spec=client.V1PodSpec(containers=[container], volumes=[shared_volume, user_volume])
                )
                spec = client.V1DeploymentSpec(
                    replicas=1,
                    template=template,
                    selector={'matchLabels': {'app': selector}}
                )
                deployment = client.V1Deployment(
                    kind='Deployment',
                    metadata=client.V1ObjectMeta(name=dep_name, namespace=KUBERNETES_NAMESPACE,
                                                 labels={'app': selector}),
                    spec=spec
                )
                app_api.create_namespaced_deployment(body=deployment, namespace=KUBERNETES_NAMESPACE)
                user_vnc.pod_name = dep_name
                user_vnc.vnc_password = vnc_pw
            if not user_vnc.url_path:
                # create service
                spec = client.V1ServiceSpec(
                    external_name=selector,
                    ports=[client.V1ServicePort(name='websocket-port', port=config.USER_VNC_PORT,
                                                target_port=config.USER_VNC_PORT)],
                    selector={'app': selector},
                    type='ClusterIP',
                )
                service = client.V1Service(
                    spec=spec,
                    metadata=client.V1ObjectMeta(
                        labels={'app': selector},
                        name=selector,
                        namespace=KUBERNETES_NAMESPACE
                    )
                )
                try:
                    core_api.create_namespaced_service(namespace=KUBERNETES_NAMESPACE, body=service)
                except ApiException as ex:
                    if ex.status != 409:  # ignore conflict (duplicate)
                        LOGGER.exception(ex)
                        raise ApiException
                # create ingress
                url_path = str(get_uuid())
                spec = client.ExtensionsV1beta1IngressSpec(
                    rules=[client.ExtensionsV1beta1IngressRule(host=config.USER_VNC_HOST,
                                                               http=client.ExtensionsV1beta1HTTPIngressRuleValue(
                                                                   paths=[
                                                                       client.ExtensionsV1beta1HTTPIngressPath(
                                                                           client.ExtensionsV1beta1IngressBackend(
                                                                               service_name=selector,
                                                                               service_port=config.USER_VNC_PORT
                                                                           ),
                                                                           path='/' + url_path
                                                                       )
                                                                   ]
                                                               ))],
                    tls=[client.ExtensionsV1beta1IngressTLS(hosts=[config.USER_VNC_HOST],
                                                            secret_name=config.USER_VNC_TLS_SECRET)],
                )
                ingress = client.ExtensionsV1beta1Ingress(metadata={'name': selector,
                                                                    'annotations': {
                                                                        'kubernetes.io/ingress.class': 'nginx',
                                                                        'nginx.ingress.kubernetes.io/proxy-read-timeout':
                                                                            '86400',
                                                                        'nginx.ingress.kubernetes.io/proxy-send-timeout':
                                                                            '86400',
                                                                    }},
                                                          spec=spec)
                need_patch = False
                try:
                    extension_api.create_namespaced_ingress(KUBERNETES_NAMESPACE, ingress)
                except ApiException as ex:
                    if ex.status != 409:  # ignore conflict (duplicate)
                        LOGGER.exception(ex)
                        raise ApiException
                    else:
                        need_patch = True
                if need_patch:
                    extension_api.patch_namespaced_ingress(selector, KUBERNETES_NAMESPACE, ingress)
                user_vnc.url_path = url_path
            user_vnc.expire_time = round(time.time() + USER_SPACE_POD_TIMEOUT)
            result['url_path'] = user_vnc.url_path
            result['vnc_password'] = user_vnc.vnc_password
            result['deployment_name'] = user_vnc.pod_name
            result['vnc_host'] = config.USER_VNC_HOST
            result['vnc_port'] = config.USER_VNC_WS_PORT
            user_vnc.save(force_update=True)
        except ApiException as ex:
            LOGGER.exception(ex)
        except Exception as ex:
            LOGGER.exception(ex)
        finally:
            if user_vnc:
                user_vnc.save(force_update=True)
            return result

    @staticmethod
    def get_user_space_pod(uuid, user, recreate=False, purge=False):
        def _recreate_space(_pod_use, _conf, _username):
            extra_command = 'rm -rf /home/{}/*;'.format(_username) if purge else ''
            resp = stream(api.connect_get_namespaced_pod_exec,
                          _pod_use,
                          KUBERNETES_NAMESPACE,
                          command=
                          ['/bin/bash', '-c',
                           'set +e;'
                           '{extra_command}'
                           'cp -r {mount_path}/* /home/{user};'
                           'chown -R {user}:{user} /home/{user}/*'.format(
                               mount_path=
                               _conf['persistent_volume']['mount_path'] + '/' +
                               _conf['task_initial_file_path'],
                               user=_username, extra_command=extra_command)],
                          stderr=True, stdin=False,
                          stdout=True, tty=False)
            LOGGER.debug(extra_command)
            return resp

        api = CoreV1Api(get_kubernetes_api_client())
        result = None
        try:
            setting = TaskSettings.objects.get(uuid=uuid)
            username = '{}_{}'.format(user.username, setting.id)
            user_storage, created = TaskStorage.objects.get_or_create(settings=setting, user=user, defaults={
                'settings': setting,
                'user': user,
                'pod_name': ''
            })
            if user_storage.pod_name:
                # try get this pod
                try:
                    pod = api.read_namespaced_pod(name=user_storage.pod_name, namespace=KUBERNETES_NAMESPACE)
                    deleting = pod.metadata.deletion_timestamp
                    # do not get users to terminating pods
                    if pod.status.phase == 'Running' and not deleting:
                        result = pod
                        user_storage.expire_time = round(time.time() + USER_SPACE_POD_TIMEOUT)
                        user_storage.save(force_update=True)
                except ApiException as ex:
                    if ex.status != 404:
                        raise Exception("Unhandled ApiException")
            conf = json.loads(setting.container_config)
            if result is None:
                # if not available, try to allocate a new one
                response = api.list_namespaced_pod(namespace=KUBERNETES_NAMESPACE,
                                                   label_selector="task={}".format(uuid))
                available = None
                # crunch available pods
                for pod in response.items:
                    num_in_use = int(pod.metadata.labels['occupied'])
                    deleting = pod.metadata.deletion_timestamp
                    if pod.status.phase == 'Running' and num_in_use < setting.max_sharing_users and not deleting:
                        available = pod
                        break
                # allocate
                if available:
                    pod_name = available.metadata.name
                    available.metadata.labels['occupied'] = str(int(available.metadata.labels['occupied']) + 1)
                    try:
                        user_dir = "/cloud_scheduler_userspace/user_{}_task_{}".format(user.id, setting.id)
                        LOGGER.debug("Create username %s", username)
                        commands = ['/bin/bash', '-c',
                                    'set +e;'
                                    'chmod 711 /cloud_scheduler_userspace;'
                                    'chmod 711 /home;'
                                    'mkdir -p {user_dir};'
                                    'useradd -u {uid} {username};'
                                    'chown {username} {user_dir};'
                                    'chmod 700 {user_dir};'
                                    'ln -s {user_dir} /home/{username};'
                                    'chown {username} /home/{username};'
                                    'chmod 700 /home/{username}'.format(
                                        user_dir=user_dir,
                                        username=username,
                                        uid=user.id + 499)]
                        LOGGER.debug(commands)
                        response = stream(api.connect_get_namespaced_pod_exec,
                                          pod_name,
                                          KUBERNETES_NAMESPACE,
                                          command=commands,
                                          stderr=True, stdin=False,
                                          stdout=True, tty=False)
                        LOGGER.debug(response)
                        if created:
                            _recreate_space(pod_name, conf, username)
                        elif recreate:
                            _recreate_space(pod_name, conf, username)
                        api.patch_namespaced_pod(pod_name, KUBERNETES_NAMESPACE, available)
                        result = available
                        user_storage.pod_name = available.metadata.name
                        user_storage.expire_time = round(time.time() + USER_SPACE_POD_TIMEOUT)
                        user_storage.save(force_update=True)
                    except ApiException as ex:
                        LOGGER.warning(ex)
            elif recreate:
                _recreate_space(result.metadata.name, conf, username)
        except Exception as ex:
            LOGGER.warning(ex)
            LOGGER.exception(ex)
        finally:
            return result

    def _job_dispatch(self):
        api = BatchV1Api(get_kubernetes_api_client())

        def _actual_work():
            idle = True
            try:
                for item in Task.objects.filter(status=TASK.SCHEDULED).order_by("create_time"):
                    idle = False
                    conf = json.loads(item.settings.container_config)
                    common_name = "task-exec-{}".format(item.uuid)
                    shared_storage_name = "shared-{}".format(item.uuid)
                    user_storage_name = "user-{}".format(item.uuid)
                    user_dir = "/cloud_scheduler_userspace/"
                    create_namespace()
                    create_userspace_pvc()
                    if not get_userspace_pvc():
                        item.status = TASK.FAILED
                        item.logs_get = True
                        item.logs = "Failed to get user space storage"
                        item.save(force_update=True)
                    else:
                        try:
                            if not config_checker(conf):
                                raise ValueError("Invalid config for TaskSettings: {}".format(item.settings.uuid))
                            # kubernetes part
                            shell = conf['shell']
                            commands = []
                            mem_limit = conf['memory_limit']
                            time_limit = item.settings.time_limit
                            working_dir = conf['working_path']
                            image = conf['image']
                            shared_pvc = conf['persistent_volume']['name']
                            shared_mount_path = conf['persistent_volume']['mount_path']
                            script_path = conf['task_script_path']

                            commands.append('mkdir -p {}'.format(working_dir))
                            commands.append('cp -r {}/* {}'.format(user_dir, working_dir))
                            # snapshot
                            commands.append('cp -r {}/* {}'.format(shared_mount_path + '/' + script_path,
                                                                   working_dir))
                            # overwrite
                            commands.append('chmod -R +x {}'.format(working_dir))
                            commands.append('cd {}'.format(working_dir))
                            commands.append('timeout --signal TERM {timeout} {shell} -c \'{commands}\''.format(
                                timeout=time_limit, shell=shell, commands=';'.join(conf['commands'])))

                            shared_mount = client.V1VolumeMount(mount_path=shared_mount_path, name=shared_storage_name,
                                                                read_only=True)
                            user_mount = client.V1VolumeMount(mount_path='/cloud_scheduler_userspace/',
                                                              name=user_storage_name,
                                                              sub_path="user_{}_task_{}".format(item.user_id,
                                                                                                item.settings_id),
                                                              read_only=True)
                            env_username = client.V1EnvVar(name="CLOUD_SCHEDULER_USER", value=item.user.username)
                            env_user_uuid = client.V1EnvVar(name="CLOUD_SCHEDULER_USER_UUID", value=item.user.uuid)
                            container_settings = {
                                'name': 'task-container',
                                'image': image,
                                'volume_mounts': [shared_mount, user_mount],
                                'command': [shell],
                                'args': ['-c', ';'.join(commands)],
                                'env': [env_username, env_user_uuid]
                            }
                            if mem_limit:
                                container_settings['resources'] = client.V1ResourceRequirements(
                                    limits={'memory': mem_limit})
                            container = client.V1Container(**container_settings)
                            persistent_volume_claim = client.V1PersistentVolumeClaimVolumeSource(claim_name=shared_pvc)
                            user_volume_claim = client.V1PersistentVolumeClaimVolumeSource(claim_name=USERSPACE_NAME)
                            volume = client.V1Volume(name=shared_storage_name,
                                                     persistent_volume_claim=persistent_volume_claim)
                            user_volume = client.V1Volume(name=user_storage_name,
                                                          persistent_volume_claim=user_volume_claim)
                            template = client.V1PodTemplateSpec(
                                metadata=client.V1ObjectMeta(labels={"task-exec": item.uuid}),
                                spec=client.V1PodSpec(restart_policy="Never",
                                                      containers=[container],
                                                      volumes=[volume, user_volume]))
                            spec = client.V1JobSpec(template=template, backoff_limit=0,
                                                    active_deadline_seconds=GLOBAL_TASK_TIME_LIMIT)
                            job = client.V1Job(api_version="batch/v1", kind="Job",
                                               metadata=client.V1ObjectMeta(name=common_name),
                                               spec=spec)
                            _ = api.create_namespaced_job(
                                namespace=KUBERNETES_NAMESPACE,
                                body=job
                            )
                            item.status = TASK.WAITING
                            item.save(force_update=True)
                        except ApiException as ex:
                            LOGGER.warning("Kubernetes ApiException %d: %s", ex.status, ex.reason)
                        except ValueError as ex:
                            LOGGER.warning(ex)
                            item.status = TASK.FAILED
                            item.save(force_update=True)
                        except Exception as ex:
                            LOGGER.error(ex)
                            item.status = TASK.FAILED
                            item.save(force_update=True)
            except Exception as ex:
                LOGGER.error(ex)
            if idle:
                time.sleep(1)

        while True:
            _actual_work()
            if self.test:
                break

    @staticmethod
    def _ttl_check(uuid):
        api = CoreV1Api(get_kubernetes_api_client())

        def expand_container(num):
            for _ in range(0, num):
                pod_name = "task-storage-{}-{}".format(item.uuid, get_short_uuid())
                shared_pvc_name = "shared-{}".format(item.uuid)
                shared_pvc = client.V1VolumeMount(mount_path=conf['persistent_volume']['mount_path'],
                                                  name=shared_pvc_name, read_only=True)
                user_storage_name = "user-{}".format(item.uuid)
                user_mount = client.V1VolumeMount(mount_path='/cloud_scheduler_userspace/', name=user_storage_name)
                container_settings = {
                    'name': 'task-storage-container',
                    'image': USER_WEBSHELL_DOCKER_IMAGE,
                    'volume_mounts': [shared_pvc, user_mount],
                }
                container = client.V1Container(**container_settings)
                pvc = client.V1PersistentVolumeClaimVolumeSource(claim_name=conf['persistent_volume']['name'])
                user_volume_claim = client.V1PersistentVolumeClaimVolumeSource(claim_name=USERSPACE_NAME)
                volume = client.V1Volume(name=shared_pvc_name, persistent_volume_claim=pvc)
                user_volume = client.V1Volume(name=user_storage_name,
                                              persistent_volume_claim=user_volume_claim)
                metadata = client.V1ObjectMeta(name=pod_name, labels={'task': uuid, 'occupied': '0'})
                spec = client.V1PodSpec(containers=[container], restart_policy='Always', volumes=[volume, user_volume])
                pod = client.V1Pod(api_version='v1', kind='Pod', metadata=metadata, spec=spec)
                try:
                    api.create_namespaced_pod(namespace=KUBERNETES_NAMESPACE, body=pod)
                except ApiException as ex:
                    LOGGER.warning(ex)

        def delete_single_container(_pod):
            try:
                LOGGER.debug("Deleting pod %s", _pod.metadata.name)
                api.delete_namespaced_pod(_pod.metadata.name, KUBERNETES_NAMESPACE)
                _pod.metadata = client.V1ObjectMeta(name=_pod.metadata.name, labels={'task': uuid + '_deleted',
                                                                                     'occupied': '0'})
                api.patch_namespaced_pod(_pod.metadata.name, KUBERNETES_NAMESPACE, _pod)
            except ApiException as _ex:
                if _ex.status != 404:
                    LOGGER.warning(_ex)

        def delete_all_containers(_resp):
            for _item in _resp.items:
                LOGGER.debug("Attempting to delete pod %s", _item.metadata.name)
                delete_single_container(_item)

        response = None
        create_namespace()
        create_userspace_pvc()
        if not get_userspace_pvc():
            LOGGER.error("Failed to obtain user space persistent volume.")
            return
        try:
            response = api.list_namespaced_pod(namespace=KUBERNETES_NAMESPACE, label_selector="task={}".format(uuid))
            item = TaskSettings.objects.get(uuid=uuid)
            conf = json.loads(item.container_config)
            idle_list = []
            usable_count = 0
            base_count = 0
            has_error = False
            for pod in response.items:
                num_in_use = int(pod.metadata.labels['occupied'])
                deleting = pod.metadata.deletion_timestamp
                if pod.status.phase == 'Running' and not deleting:
                    base_count += 1
                    if num_in_use < item.max_sharing_users:
                        usable_count += 1
                        if num_in_use == 0:
                            idle_list.append(pod)
                elif pod.status.phase == 'Pending':
                    usable_count += 1
                    base_count += 1
                elif pod.status.phase == 'Succeeded' or pod.status.phase == 'Failed' or pod.status.phase == 'Unknown':
                    has_error = True
                    break

            if has_error:
                # if has error, stop checking this task
                delete_all_containers(response)
                LOGGER.error("Task %s is not runnable, please check settings", uuid)
                schedule.clear(uuid)
                return
            # initial bootstrap
            if base_count <= item.replica:
                expand_container(item.replica - base_count)
            # expand if usable too few
            if usable_count < 1:
                expand_container(base_count)
            # shrink if too many idles
            if base_count > item.replica and len(idle_list) > base_count // 2:
                delete_single_container(idle_list[0])
            LOGGER.debug("TTL CHECK with %s, usable: %d, total: %d, idle: %d",
                         uuid, usable_count, base_count, len(idle_list))

        except TaskSettings.DoesNotExist:
            # delete all related pods
            delete_all_containers(response)
            schedule.clear(uuid)
        except ApiException as ex:
            LOGGER.warning(ex)

    def schedule_task_settings(self, item):
        try:
            if config_checker(json.loads(item.container_config)):
                schedule.clear(item.uuid)
                schedule.every(item.ttl_interval).seconds.do(self._run_job,
                                                             fn=self._ttl_check, uuid=item.uuid).tag(item.uuid)
            else:
                LOGGER.warning("Task %s has invalid settings, ignored...", item.uuid)
        except ValueError:
            LOGGER.warning("Task %s has settings that is not JSON, ignored...", item.uuid)

    def dispatch(self):
        for item in TaskSettings.objects.all():
            self.schedule_task_settings(item)
        self.ready = True
        while True:
            schedule.run_pending()
            time.sleep(0.01)
            if self.test:
                break

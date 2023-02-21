import datetime
import functools
import hashlib
import logging
import time

from django.core.exceptions import ValidationError
from django.db.utils import OperationalError

from kolibri.core.device.utils import get_device_setting
from kolibri.core.discovery.hooks import NetworkLocationDiscoveryHook
from kolibri.core.discovery.models import ConnectionStatus
from kolibri.core.discovery.models import DynamicNetworkLocation
from kolibri.core.discovery.models import NetworkLocation
from kolibri.core.discovery.models import StaticNetworkLocation
from kolibri.core.discovery.utils.network.broadcast import KolibriInstance
from kolibri.core.discovery.utils.network.connections import update_network_location
from kolibri.core.tasks.decorators import register_task

logger = logging.getLogger(__name__)

CONNECTION_RESET_JOB_ID = "1000"
CONNECTION_FAULT_LIMIT = 10


def _store_dynamic_instance(broadcast_id, instance):
    """
    :param broadcast_id: The hex UUID of the broadcast during which the instance was discovered
    :param instance: The new Kolibri instance that has been discovered
    :type instance: kolibri.core.discovery.utils.network.broadcast.KolibriInstance
    :rtype: NetworkLocation
    """
    network_location = None
    logger.debug(
        "Creating `DynamicNetworkLocation` for instance {}".format(instance.id)
    )
    try:
        network_location, _ = DynamicNetworkLocation.objects.update_or_create(
            dict(
                base_url=instance.base_url,
                broadcast_id=broadcast_id,
                ip_address=instance.ip,
                **instance.device_info
            ),
            pk=instance.zeroconf_id,
        )
    except ValidationError:
        import traceback

        logger.warning(
            """
                A new Kolibri instance '%s' was seen on the zeroconf network,
                but we had trouble getting the information we needed about it.
                Device info:
                %s
                The following exception was raised:
                %s
                """
            % (
                instance.zeroconf_id,
                instance.device_info,
                traceback.format_exc(limit=1),
            )
        )
    except OperationalError as e:
        if "database is locked" not in str(e):
            raise
        logger.debug(
            "Encountered locked database while creating `DynamicNetworkLocation`"
        )
    return network_location


def _dispatch_hooks(network_location, is_connected):
    """
    :type network_location: NetworkLocation
    :type is_connected: bool
    """
    for hook in NetworkLocationDiscoveryHook.registered_hooks:
        # we catch all errors because as a rule of thumb,
        # we don't want hooks to fail everything else
        try:
            if is_connected:
                hook.on_connect(network_location)
            else:
                hook.on_disconnect(network_location)
        except Exception as e:
            logger.error(
                "{}.{} hook failed".format(
                    "on_connect" if is_connected else "on_disconnect",
                    hook.__class__.__name__,
                ),
                exc_info=e,
            )


def _update_connection_status(network_location):
    """
    Performs the call to update a specific network location's status, and dispatches hooks
    accordingly depending on connection status

    :type network_location: NetworkLocation
    :return: the new status
    """
    prior_status = network_location.connection_status

    try:
        update_network_location(network_location)
    except Exception as e:
        logger.error(e)
        logger.warning(
            "Failed to update connection status for {} location {}".format(
                "dynamic" if network_location.dynamic else "static", network_location.id
            )
        )

    # don't call hooks if status didn't change to/from Okay
    new_status = network_location.connection_status
    if new_status != prior_status and ConnectionStatus.Okay in (
        prior_status,
        new_status,
    ):
        _dispatch_hooks(network_location, new_status == ConnectionStatus.Okay)

    return new_status


def hydrate_instance(func):
    """
    Small decorator that turns the `KolibriInstance` dictionary/JSON into a `KolibriInstance`
    """

    @functools.wraps(func)
    def wrapped(*args):
        new_args = list(args)
        new_args[1] = KolibriInstance.from_dict(args[1])
        return func(*new_args)

    return wrapped


def generate_job_id(*args):
    """
    Utility for preventing job duplicates by hashing arguments to create job IDs
    """
    md5 = hashlib.md5()
    for arg in args:
        md5.update(arg.encode("utf-8"))
    return md5.hexdigest()


def _enqueue_network_location_update_with_backoff(network_location):
    """
    Enqueues another delayed job of `perform_network_location_update` with an exponential delay
    dependent on how many connection faults have occurred
    :type network_location: NetworkLocation
    """
    # exponential backoff depending on how many faults/attempts we've had
    next_attempt = datetime.timedelta(minutes=2 ** network_location.connection_faults)
    perform_network_location_update.enqueue_in(
        next_attempt,
        job_id=generate_job_id("connect", network_location.id),
        args=(network_location.id,),
    )


@register_task
def perform_network_location_update(network_location_id):
    """
    Updates the connection status for the network location, and dispatches hooks if applicable
    :param network_location_id: The hex ID or UUID of the network location to update
    """
    try:
        network_location = NetworkLocation.objects.get(id=network_location_id)
    except NetworkLocation.DoesNotExist:
        # may have been removed if its dynamic
        return

    # slightly awkward optimization-- if we're a SoUD and the location is too, don't connect
    if (
        get_device_setting("subset_of_users_device", default=False)
        and network_location.subset_of_users_device
    ):
        return

    prior_status = network_location.connection_status
    new_status = _update_connection_status(network_location)

    # if we've made enough connection attempts or  prior status was conflict and it hasn't changed,
    # that's the end of checking for updates
    if network_location.connection_faults >= CONNECTION_FAULT_LIMIT or (
        new_status == ConnectionStatus.Conflict
        and prior_status == ConnectionStatus.Conflict
    ):
        return

    # enqueue another attempt if the connection failed
    if new_status != ConnectionStatus.Okay:
        _enqueue_network_location_update_with_backoff(network_location)


@register_task
@hydrate_instance
def add_dynamic_network_location(broadcast_id, instance):
    """
    Handles adding a new instance when discovered over Zeroconf
    :param broadcast_id: The hex UUID of the broadcast during which the instance was discovered
    :param instance: The new Kolibri instance that has been discovered
    :type instance: kolibri.core.discovery.utils.network.broadcast.KolibriInstance
    """
    attempts = 0
    network_location = None
    while network_location is None and attempts <= 5:
        network_location = _store_dynamic_instance(broadcast_id, instance)
        attempts += 1
        time.sleep(0.1)

    # if we couldn't store it, that's the end
    if network_location is not None:
        new_status = _update_connection_status(network_location)
        # enqueue another attempt if the connection failed
        if new_status != ConnectionStatus.Okay:
            _enqueue_network_location_update_with_backoff(network_location)


@register_task
@hydrate_instance
def remove_dynamic_network_location(broadcast_id, instance):
    """
    Handles removing an instance when it disappears from Zeroconf
    :param broadcast_id: The hex UUID of the broadcast during which the instance was discovered
    :param instance: The new Kolibri instance that has been discovered
    :type instance: kolibri.core.discovery.utils.network.broadcast.KolibriInstance
    """
    try:
        network_location = DynamicNetworkLocation.objects.get(
            pk=instance.zeroconf_id, broadcast_id=broadcast_id
        )
    except NetworkLocation.DoesNotExist:
        return

    _dispatch_hooks(network_location, False)
    network_location.delete()


@register_task(job_id=CONNECTION_RESET_JOB_ID)
def reset_connection_states(broadcast_id):
    """
    Handles resetting all connection states when a network change occurs
    :param broadcast_id: The hex UUID of the new broadcast
    """
    # dispatch disconnect hooks
    for network_location in NetworkLocation.objects.exclude(
        broadcast_id=broadcast_id
    ).filter(connection_status=ConnectionStatus.Okay):
        _dispatch_hooks(network_location, False)

    # remove any dynamic locations that don't match the current broadcast
    DynamicNetworkLocation.objects.exclude(broadcast_id=broadcast_id).delete()
    # reset the connection status for each
    NetworkLocation.objects.exclude(broadcast_id=broadcast_id).update(
        connection_status=ConnectionStatus.Unknown
    )

    # enqueue update tasks for all static locations
    for static_location_id in StaticNetworkLocation.objects.all().values_list(
        "id", flat=True
    ):
        perform_network_location_update.enqueue(
            job_id=generate_job_id("connect", static_location_id),
            args=(static_location_id,),
        )

# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from stevedore import enabled

from blazar.db import api as db_api
from blazar.db import exceptions as db_ex
from blazar import exceptions as common_ex
from blazar import manager
from blazar.manager import exceptions
from blazar import monitor
from blazar.notification import api as notification_api
from blazar import status
from blazar.utils import service as service_utils
from blazar.utils import trusts

manager_opts = [
    cfg.ListOpt('plugins',
                default=['dummy.vm.plugin'],
                help='All plugins to use (one for every resource type to '
                     'support.)'),
    cfg.IntOpt('minutes_before_end_lease',
               default=60,
               min=0,
               help='Minutes prior to the end of a lease in which actions '
                    'like notification and snapshot are taken. If this is '
                    'set to 0, then these actions are not taken.'),
    cfg.IntOpt('event_max_retries',
               default=1,
               min=0,
               max=50,
               help='Number of times to retry an event action.')
]

CONF = cfg.CONF
CONF.register_opts(manager_opts, 'manager')
LOG = logging.getLogger(__name__)

LEASE_DATE_FORMAT = "%Y-%m-%d %H:%M"

EVENT_INTERVAL = 10


class ManagerService(service_utils.RPCServer):
    """Service class for the blazar-manager service.

    Responsible for working with Blazar DB, scheduling logic, running events,
    working with plugins, etc.
    """

    def __init__(self):
        target = manager.get_target()
        super(ManagerService, self).__init__(target)
        self.plugins = self._get_plugins()
        self.resource_actions = self._setup_actions()
        self.monitors = monitor.load_monitors(self.plugins)

    def start(self):
        super(ManagerService, self).start()
        self.tg.add_timer(EVENT_INTERVAL, self._event)
        for m in self.monitors:
            m.start_monitoring()

    def _get_plugins(self):
        """Return dict of resource-plugin class pairs."""
        config_plugins = CONF.manager.plugins
        plugins = {}

        extension_manager = enabled.EnabledExtensionManager(
            check_func=lambda ext: ext.name in config_plugins,
            namespace='blazar.resource.plugins',
            invoke_on_load=False
        )

        invalid_plugins = (set(config_plugins) -
                           set([ext.name for ext
                                in extension_manager.extensions]))
        if invalid_plugins:
            raise common_ex.BlazarException('Invalid plugin names are '
                                            'specified: %s' % invalid_plugins)

        for ext in extension_manager.extensions:
            try:
                plugin_obj = ext.plugin()
            except Exception as e:
                LOG.warning("Could not load {0} plugin "
                            "for resource type {1} '{2}'".format(
                                ext.name, ext.plugin.resource_type, e))
            else:
                if plugin_obj.resource_type in plugins:
                    msg = ("You have provided several plugins for "
                           "one resource type in configuration file. "
                           "Please set one plugin per resource type.")
                    raise exceptions.PluginConfigurationError(error=msg)

                plugins[plugin_obj.resource_type] = plugin_obj
        return plugins

    def _setup_actions(self):
        """Setup actions for each resource type supported.

        BasePlugin interface provides only on_start and on_end behaviour now.
        If there are some configs needed by plugin, they should be returned
        from get_plugin_opts method. These flags are registered in
        [resource_type] group of configuration file.
        """
        actions = {}

        for resource_type, plugin in self.plugins.items():
            plugin = self.plugins[resource_type]
            CONF.register_opts(plugin.get_plugin_opts(), group=resource_type)

            actions[resource_type] = {}
            actions[resource_type]['on_start'] = plugin.on_start
            actions[resource_type]['on_end'] = plugin.on_end
            actions[resource_type]['before_end'] = plugin.before_end
            plugin.setup(None)
        return actions

    @service_utils.with_empty_context
    def _event(self):
        """Tries to commit event.

        If there is an event in Blazar DB to be done, do it and change its
        status to 'DONE'.
        """
        LOG.debug('Trying to get event from DB.')
        events = db_api.event_get_all_sorted_by_filters(
            sort_key='time',
            sort_dir='asc',
            filters={'status': status.event.UNDONE,
                     'time': {'op': 'le',
                              'border': datetime.datetime.utcnow()}}
        )

        if not events:
            return

        LOG.info("Trying to execute events: %s", events)
        for event in events:
            if not status.LeaseStatus.is_stable(event['lease_id']):
                LOG.info("Skip event %s because the status of the lease %s "
                         "is still transitional", event, event['lease_id'])
                continue
            db_api.event_update(event['id'],
                                {'status': status.event.IN_PROGRESS})
            try:
                eventlet.spawn_n(
                    service_utils.with_empty_context(self._exec_event),
                    event)
            except Exception:
                db_api.event_update(event['id'],
                                    {'status': status.event.ERROR})
                LOG.exception('Error occurred while event %s handling.',
                              event['id'])

    def _exec_event(self, event):
        """Execute an event function"""
        event_fn = getattr(self, event['event_type'], None)
        if event_fn is None:
            raise exceptions.EventError(
                error='Event type %s is not supported'
                      % event['event_type'])
        try:
            event_fn(lease_id=event['lease_id'], event_id=event['id'])
        except common_ex.InvalidStatus:
            now = datetime.datetime.utcnow()
            if now < event['time'] + datetime.timedelta(
                    seconds=CONF.manager.event_max_retries * 10):
                # Set the event status UNDONE for retrying the event
                db_api.event_update(event['id'],
                                    {'status': status.event.UNDONE})
            else:
                db_api.event_update(event['id'],
                                    {'status': status.event.ERROR})
                LOG.exception('Error occurred while handling %s event for '
                              'lease %s.', event['event_type'],
                              event['lease_id'])
        except Exception:
            db_api.event_update(event['id'],
                                {'status': status.event.ERROR})
            LOG.exception('Error occurred while handling %s event for '
                          'lease %s.', event['event_type'], event['lease_id'])
        else:
            lease = db_api.lease_get(event['lease_id'])
            with trusts.create_ctx_from_trust(lease['trust_id']) as ctx:
                self._send_notification(
                    lease, ctx, events=['event.%s' % event['event_type']])

    def _date_from_string(self, date_string, date_format=LEASE_DATE_FORMAT):
        try:
            date = datetime.datetime.strptime(date_string, date_format)
        except ValueError:
            raise exceptions.InvalidDate(date=date_string,
                                         date_format=date_format)
        return date

    def validate_params(self, values, required_params):
        if isinstance(required_params, list):
            required_params = set(required_params)
        missing_attr = required_params - set(values.keys())
        if missing_attr:
            raise exceptions.MissingParameter(param=', '.join(missing_attr))

    def get_lease(self, lease_id):
        return db_api.lease_get(lease_id)

    def list_leases(self, project_id=None, query=None):
        return db_api.lease_list(project_id)

    def create_lease(self, lease_values):
        """Create a lease with reservations.

        Return either the model of created lease or None if any error.
        """
        lease_values['status'] = status.lease.CREATING

        try:
            trust_id = lease_values.pop('trust_id')
        except KeyError:
            raise exceptions.MissingTrustId()

        self.validate_params(lease_values, ['name', 'start_date', 'end_date'])

        # Remove and keep event and reservation values
        events = lease_values.pop("events", [])
        reservations = lease_values.pop("reservations", [])
        for res in reservations:
            self.validate_params(res, ['resource_type'])

        # Create the lease without the reservations
        start_date = lease_values['start_date']
        end_date = lease_values['end_date']

        now = datetime.datetime.utcnow()
        now = datetime.datetime(now.year,
                                now.month,
                                now.day,
                                now.hour,
                                now.minute)
        if start_date == 'now':
            start_date = now
        else:
            start_date = self._date_from_string(start_date)
        end_date = self._date_from_string(end_date)

        if start_date < now:
            raise common_ex.InvalidInput(
                'Start date must be later than current date')

        if end_date <= start_date:
            raise common_ex.InvalidInput(
                'End date must be later than start date.')

        with trusts.create_ctx_from_trust(trust_id) as ctx:
            # NOTE(priteau): We should not get user_id from ctx, because we are
            # in the context of the trustee (blazar user).
            # lease_values['user_id'] is set in blazar/api/v1/service.py
            lease_values['project_id'] = ctx.project_id
            lease_values['start_date'] = start_date
            lease_values['end_date'] = end_date

            events.append({'event_type': 'start_lease',
                           'time': start_date,
                           'status': status.event.UNDONE})
            events.append({'event_type': 'end_lease',
                           'time': end_date,
                           'status': status.event.UNDONE})

            before_end_date = lease_values.get('before_end_date', None)
            if before_end_date:
                # incoming param. Validation check
                try:
                    before_end_date = self._date_from_string(
                        before_end_date)
                    self._check_date_within_lease_limits(before_end_date,
                                                         lease_values)
                except common_ex.BlazarException as e:
                    LOG.error("Invalid before_end_date param. %s", str(e))
                    raise e
            elif CONF.manager.minutes_before_end_lease > 0:
                delta = datetime.timedelta(
                    minutes=CONF.manager.minutes_before_end_lease)
                before_end_date = lease_values['end_date'] - delta

            if before_end_date:
                event = {'event_type': 'before_end_lease',
                         'status': status.event.UNDONE}
                events.append(event)
                self._update_before_end_event_date(event, before_end_date,
                                                   lease_values)

            try:
                if trust_id:
                    lease_values.update({'trust_id': trust_id})
                lease = db_api.lease_create(lease_values)
                lease_id = lease['id']
            except db_ex.BlazarDBDuplicateEntry:
                LOG.exception('Cannot create a lease - duplicated lease name')
                raise exceptions.LeaseNameAlreadyExists(
                    name=lease_values['name'])
            except db_ex.BlazarDBException:
                LOG.exception('Cannot create a lease')
                raise
            else:
                try:
                    for reservation in reservations:
                        reservation['lease_id'] = lease['id']
                        reservation['start_date'] = lease['start_date']
                        reservation['end_date'] = lease['end_date']
                        self._create_reservation(reservation)
                except Exception:
                    LOG.exception("Failed to create reservation for a lease. "
                                  "Rollback the lease and associated "
                                  "reservations")
                    db_api.lease_destroy(lease_id)
                    raise

                try:
                    for event in events:
                        event['lease_id'] = lease['id']
                        db_api.event_create(event)
                except (exceptions.UnsupportedResourceType,
                        common_ex.BlazarException):
                    LOG.exception("Failed to create event for a lease. "
                                  "Rollback the lease and associated "
                                  "reservations")
                    db_api.lease_destroy(lease_id)
                    raise

                else:
                    db_api.lease_update(
                        lease_id,
                        {'status': status.lease.PENDING})
                    lease = db_api.lease_get(lease_id)
                    self._send_notification(lease, ctx, events=['create'])
                    return lease

    @status.lease.lease_status(
        transition=status.lease.UPDATING, result_in=status.lease.STABLE)
    def update_lease(self, lease_id, values):
        if not values:
            return db_api.lease_get(lease_id)

        if len(values) == 1 and 'name' in values:
            db_api.lease_update(lease_id, values)
            return db_api.lease_get(lease_id)

        lease = db_api.lease_get(lease_id)
        start_date = values.get(
            'start_date',
            datetime.datetime.strftime(lease['start_date'], LEASE_DATE_FORMAT))
        end_date = values.get(
            'end_date',
            datetime.datetime.strftime(lease['end_date'], LEASE_DATE_FORMAT))
        before_end_date = values.get('before_end_date', None)

        now = datetime.datetime.utcnow()
        now = datetime.datetime(now.year,
                                now.month,
                                now.day,
                                now.hour,
                                now.minute)
        if start_date == 'now':
            start_date = now
        else:
            start_date = self._date_from_string(start_date)
        if end_date == 'now':
            end_date = now
        else:
            end_date = self._date_from_string(end_date)

        values['start_date'] = start_date
        values['end_date'] = end_date

        if (lease['start_date'] < now and
                values['start_date'] != lease['start_date']):
            raise common_ex.InvalidInput(
                'Cannot modify the start date of already started leases')

        if (lease['start_date'] > now and
                values['start_date'] < now):
            raise common_ex.InvalidInput(
                'Start date must be later than current date')

        if lease['end_date'] < now:
            raise common_ex.InvalidInput(
                'Terminated leases can only be renamed')

        if (values['end_date'] < now or
           values['end_date'] < values['start_date']):
            raise common_ex.InvalidInput(
                'End date must be later than current and start date')

        with trusts.create_ctx_from_trust(lease['trust_id']):
            if before_end_date:
                try:
                    before_end_date = self._date_from_string(before_end_date)
                    self._check_date_within_lease_limits(before_end_date,
                                                         values)
                except common_ex.BlazarException as e:
                    LOG.error("Invalid before_end_date param. %s", str(e))
                    raise e

            # TODO(frossigneux) rollback if an exception is raised
            reservations = values.get('reservations', [])
            reservations_db = db_api.reservation_get_all_by_lease_id(lease_id)
            try:
                invalid_ids = set([r['id'] for r in reservations]).difference(
                    [r['id'] for r in reservations_db])
            except KeyError:
                raise exceptions.MissingParameter(param='reservation ID')

            if invalid_ids:
                raise common_ex.InvalidInput(
                    'Please enter valid reservation IDs. Invalid reservation '
                    'IDs are: %s' % ','.join([str(id) for id in invalid_ids]))

            for reservation in (reservations_db):
                v = {}
                v['start_date'] = values['start_date']
                v['end_date'] = values['end_date']
                try:
                    v.update([r for r in reservations
                              if r['id'] == reservation['id']].pop())
                except IndexError:
                    pass
                resource_type = v.get('resource_type',
                                      reservation['resource_type'])
                if resource_type != reservation['resource_type']:
                    raise exceptions.CantUpdateParameter(
                        param='resource_type')
                self.plugins[resource_type].update_reservation(
                    reservation['id'], v)

        event = db_api.event_get_first_sorted_by_filters(
            'lease_id',
            'asc',
            {
                'lease_id': lease_id,
                'event_type': 'start_lease'
            }
        )
        if not event:
            raise common_ex.BlazarException(
                'Start lease event not found')
        db_api.event_update(event['id'], {'time': values['start_date']})

        event = db_api.event_get_first_sorted_by_filters(
            'lease_id',
            'asc',
            {
                'lease_id': lease_id,
                'event_type': 'end_lease'
            }
        )
        if not event:
            raise common_ex.BlazarException(
                'End lease event not found')
        db_api.event_update(event['id'], {'time': values['end_date']})

        notifications = ['update']
        self._update_before_end_event(lease, values, notifications,
                                      before_end_date)

        try:
            del values['reservations']
        except KeyError:
            pass
        db_api.lease_update(lease_id, values)

        lease = db_api.lease_get(lease_id)
        with trusts.create_ctx_from_trust(lease['trust_id']) as ctx:
            self._send_notification(lease, ctx, events=notifications)

        return lease

    @status.lease.lease_status(transition=status.lease.DELETING,
                               result_in=(status.lease.ERROR,))
    def delete_lease(self, lease_id):
        lease = self.get_lease(lease_id)
        if (datetime.datetime.utcnow() >= lease['start_date'] and
                datetime.datetime.utcnow() <= lease['end_date']):
            start_event = db_api.event_get_first_sorted_by_filters(
                'lease_id',
                'asc',
                {
                    'lease_id': lease_id,
                    'event_type': 'start_lease'
                }
            )
            if not start_event:
                raise common_ex.BlazarException(
                    'start_lease event for lease %s not found' % lease_id)
            end_event = db_api.event_get_first_sorted_by_filters(
                'lease_id',
                'asc',
                {
                    'lease_id': lease_id,
                    'event_type': 'end_lease',
                    'status': status.event.UNDONE
                }
            )
            if not end_event:
                raise common_ex.BlazarException(
                    'end_lease event for lease %s not found' % lease_id)
            db_api.event_update(end_event['id'],
                                {'status': status.event.IN_PROGRESS})

        with trusts.create_ctx_from_trust(lease['trust_id']) as ctx:
            for reservation in lease['reservations']:
                if reservation['status'] != status.reservation.DELETED:
                    plugin = self.plugins[reservation['resource_type']]
                    try:
                        plugin.on_end(reservation['resource_id'])
                    except (db_ex.BlazarDBException, RuntimeError):
                        LOG.exception("Failed to delete a reservation "
                                      "for a lease.")
                        raise
            db_api.lease_destroy(lease_id)
            self._send_notification(lease, ctx, events=['delete'])

    @status.lease.lease_status(
        transition=status.lease.STARTING,
        result_in=(status.lease.ACTIVE, status.lease.ERROR))
    def start_lease(self, lease_id, event_id):
        lease = self.get_lease(lease_id)
        with trusts.create_ctx_from_trust(lease['trust_id']):
            self._basic_action(lease_id, event_id, 'on_start',
                               status.reservation.ACTIVE)

    @status.lease.lease_status(
        transition=status.lease.TERMINATING,
        result_in=(status.lease.TERMINATED, status.lease.ERROR))
    def end_lease(self, lease_id, event_id):
        lease = self.get_lease(lease_id)
        with trusts.create_ctx_from_trust(lease['trust_id']):
            self._basic_action(lease_id, event_id, 'on_end',
                               status.reservation.DELETED)

    def before_end_lease(self, lease_id, event_id):
        lease = self.get_lease(lease_id)
        with trusts.create_ctx_from_trust(lease['trust_id']):
            self._basic_action(lease_id, event_id, 'before_end')

    def _basic_action(self, lease_id, event_id, action_time,
                      reservation_status=None):
        """Commits basic lease actions such as starting and ending."""
        lease = self.get_lease(lease_id)

        event_status = status.event.DONE
        for reservation in lease['reservations']:
            resource_type = reservation['resource_type']
            try:
                if reservation_status is not None:
                    if not status.reservation.is_valid_transition(
                            reservation['status'], reservation_status):
                        raise common_ex.InvalidStatus
                self.resource_actions[resource_type][action_time](
                    reservation['resource_id']
                )
            except common_ex.BlazarException:
                LOG.exception("Failed to execute action %(action)s "
                              "for lease %(lease)s",
                              {'action': action_time,
                               'lease': lease_id})
                event_status = status.event.ERROR
                db_api.reservation_update(
                    reservation['id'],
                    {'status': status.reservation.ERROR})
            else:
                if reservation_status is not None:
                    db_api.reservation_update(reservation['id'],
                                              {'status': reservation_status})

        db_api.event_update(event_id, {'status': event_status})

        return event_status

    def _create_reservation(self, values):
        resource_type = values['resource_type']
        if resource_type not in self.plugins:
            raise exceptions.UnsupportedResourceType(
                resource_type=resource_type)
        reservation_values = {
            'lease_id': values['lease_id'],
            'resource_type': resource_type,
            'status': status.reservation.PENDING
        }
        reservation = db_api.reservation_create(reservation_values)
        resource_id = self.plugins[resource_type].reserve_resource(
            reservation['id'],
            values
        )
        db_api.reservation_update(reservation['id'],
                                  {'resource_id': resource_id})

    def _send_notification(self, lease, ctx, events=[]):
        payload = notification_api.format_lease_payload(lease)

        for event in events:
            notification_api.send_lease_notification(ctx, payload,
                                                     'lease.%s' % event)

    def _check_date_within_lease_limits(self, date, lease):
        if not lease['start_date'] < date < lease['end_date']:
            raise common_ex.NotAuthorized(
                'Datetime is out of lease limits')

    def _update_before_end_event_date(self, event, before_end_date, lease):
        event['time'] = before_end_date
        if event['time'] < lease['start_date']:
            LOG.warning("Start_date greater than before_end_date. "
                        "Setting before_end_date to %(start_date)s for "
                        "lease %(id_name)s",
                        {'start_date': lease['start_date'],
                         'id_name': lease.get('id', lease.get('name'))})
            event['time'] = lease['start_date']

    def _update_before_end_event(self, old_lease, new_lease,
                                 notifications, before_end_date=None):
        event = db_api.event_get_first_sorted_by_filters(
            'lease_id',
            'asc',
            {
                'lease_id': old_lease['id'],
                'event_type': 'before_end_lease'
            }
        )
        if event:
            # NOTE(casanch1) do nothing if the event does not exist.
            # This is for backward compatibility
            update_values = {}
            if not before_end_date:
                # before_end_date needs to be calculated based on
                # previous delta
                prev_before_end_delta = old_lease['end_date'] - event['time']
                before_end_date = new_lease['end_date'] - prev_before_end_delta

            self._update_before_end_event_date(update_values, before_end_date,
                                               new_lease)
            if event['status'] == status.event.DONE:
                update_values['status'] = status.event.UNDONE
                notifications.append('event.before_end_lease.stop')

            db_api.event_update(event['id'], update_values)

    def __getattr__(self, name):
        """RPC Dispatcher for plugins methods."""

        fn = None
        try:
            resource_type, method = name.rsplit(':', 1)
        except ValueError:
            # NOTE(sbauza) : the dispatcher needs to know which plugin to use,
            #  raising error if consequently not
            raise AttributeError(name)
        try:
            try:
                fn = getattr(self.plugins[resource_type], method)
            except KeyError:
                LOG.error("Plugin with resource type %s not found",
                          resource_type)
                raise exceptions.UnsupportedResourceType(
                    resource_type=resource_type)
        except AttributeError:
            LOG.error("Plugin %s doesn't include method %s",
                      self.plugins[resource_type], method)
        if fn is not None:
            return fn
        raise AttributeError(name)

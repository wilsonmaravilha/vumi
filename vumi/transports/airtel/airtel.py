# -*- test-case-name: vumi.transports.airtel.tests.test_airtel -*-

import json

from twisted.internet.defer import inlineCallbacks
from twisted.web import http

from vumi.transports.httprpc import HttpRpcTransport
from vumi.components.session import SessionManager
from vumi.message import TransportUserMessage
from vumi import log
from vumi.config import ConfigInt, ConfigText, ConfigBool, ConfigDict


class AirtelUSSDTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    airtel_username = ConfigText('The username for this transport',
                                 default=None, static=True)
    airtel_password = ConfigText('The password for this transport',
                                 default=None, static=True)
    airtel_charge = ConfigBool(
        'Whether or not to charge for the responses sent.', required=False,
        default=False, static=True)
    airtel_charge_amount = ConfigInt('How much to charge', default=0,
                                     required=False, static=True)
    redis_manager = ConfigDict('Parameters to connect to Redis with.',
                               default={}, required=False, static=True)
    ussd_session_timeout = ConfigInt('Max length of a USSD session',
                                     default=60 * 10, required=False,
                                     static=True)


class AirtelUSSDTransport(HttpRpcTransport):
    """
    Client implementation for the Comviva Flares HTTP Pull API.
    Based on Flares 1.5.0, document version 1.2.0
    """

    transport_type = 'ussd'
    content_type = 'text/plain; charset=utf-8'
    ENCODING = 'utf-8'
    CONFIG_CLASS = AirtelUSSDTransportConfig
    EXPECTED_AUTH_FIELDS = set(['userid', 'password'])
    EXPECTED_CLEANUP_FIELDS = set(['SessionID', 'msisdn', 'clean', 'error'])
    EXPECTED_USSD_FIELDS = set(['SessionID', 'MSISDN', 'MSC', 'input'])

    @inlineCallbacks
    def setup_transport(self):
        super(AirtelUSSDTransport, self).setup_transport()
        config = self.get_static_config()
        r_prefix = "vumi.transports.airtel:%s" % self.transport_name
        self.session_manager = yield SessionManager.from_redis_config(
            config.redis_manager, r_prefix,
            config.ussd_session_timeout)

    def is_cleanup(self, request):
        return 'clean' in request.args

    def requires_auth(self):
        config = self.get_static_config()
        return (None not in (config.airtel_username, config.airtel_password))

    def is_authenticated(self, request):
        config = self.get_static_config()
        if self.EXPECTED_AUTH_FIELDS.issubset(request.args):
            username = request.args['userid'][0]
            password = request.args['password'][0]
            auth = (username == config.airtel_username and
                    password == config.airtel_password)
            if not auth:
                log.msg('Invalid authentication credentials: %s:%s' % (
                        username, password))
            return auth

    def handle_bad_request(self, message_id, request, errors):
        log.msg('Unhappy incoming message: %s' % (errors,))
        return self.finish_request(message_id, json.dumps(errors),
                                   code=http.BAD_REQUEST)

    def handle_raw_inbound_message(self, message_id, request):
        if self.requires_auth() and not self.is_authenticated(request):
            self.finish_request(message_id, 'Forbidden', code=http.FORBIDDEN)
            return

        if self.is_cleanup(request):
            return self.handle_cleanup_request(message_id, request)
        return self.handle_ussd_request(message_id, request)

    @inlineCallbacks
    def handle_cleanup_request(self, message_id, request):
        if self.requires_auth():
            fields = self.EXPECTED_CLEANUP_FIELDS.union(
                self.EXPECTED_AUTH_FIELDS)
        else:
            fields = self.EXPECTED_CLEANUP_FIELDS

        values, errors = self.get_field_values(request, fields)
        if errors:
            self.handle_bad_request(message_id, request, errors)
            return

        session_id = values['SessionID']
        session = yield self.session_manager.load_session(session_id)
        if not session:
            log.warning('Received cleanup for unknown session: %s' % (
                        session_id,))
            self.finish_request(message_id, 'Unknown Session', code=http.OK)
            return

        from_addr = values['msisdn']
        to_addr = session['to_addr']
        session_event = TransportUserMessage.SESSION_CLOSE
        yield self.session_manager.clear_session(session_id)
        yield self.publish_message(
            message_id=message_id,
            content='',
            to_addr=to_addr,
            from_addr=from_addr,
            provider='airtel',
            session_event=session_event,
            transport_type=self.transport_type,
            transport_metadata={
                'airtel': {
                    'clean': values['clean'],
                    'error': values['error'],
                },
            })
        self.finish_request(message_id, '', code=http.OK)

    @inlineCallbacks
    def handle_ussd_request(self, message_id, request):
        if self.requires_auth():
            fields = self.EXPECTED_USSD_FIELDS.union(
                self.EXPECTED_AUTH_FIELDS)
        else:
            fields = self.EXPECTED_USSD_FIELDS

        values, errors = self.get_field_values(request, fields)
        if errors:
            self.handle_bad_request(message_id, request, errors)
            return

        session_id = values['SessionID']
        from_addr = values['MSISDN']

        session = yield self.session_manager.load_session(session_id)
        if session:
            to_addr = session['to_addr']
            yield self.session_manager.save_session(session_id, session)
            session_event = TransportUserMessage.SESSION_RESUME
            content = values['input']
        else:
            # Airtel doesn't provide us with the full to_addr, the start *
            # and ending # are omitted, add those again so we can use it
            # for internal routing.
            to_addr = '*%s#' % (values['input'],)
            yield self.session_manager.create_session(
                session_id, from_addr=from_addr, to_addr=to_addr)
            session_event = TransportUserMessage.SESSION_NEW
            content = ''

        yield self.publish_message(
            message_id=message_id,
            content=content,
            to_addr=to_addr,
            from_addr=from_addr,
            provider='airtel',
            session_event=session_event,
            transport_type=self.transport_type,
            transport_metadata={
                'airtel': {
                    'MSC': values['MSC'],
                },
            })

    def handle_outbound_message(self, message):
        config = self.get_static_config()
        missing_fields = self.ensure_message_values(
            message, ['in_reply_to', 'content'])

        if missing_fields:
            return self.reject_message(message, missing_fields)

        if message['session_event'] == TransportUserMessage.SESSION_CLOSE:
            free_flow = 'FB'
        else:
            free_flow = 'FC'

        headers = {
            'Freeflow': [free_flow],
            'charge': [('Y' if config.airtel_charge else 'N')],
            'amount': [str(config.airtel_charge_amount)],
        }

        if self.noisy:
            log.debug('in_reply_to: %s' % (message['in_reply_to'],))
            log.debug('content: %s' % (message['content'],))
            log.debug('Response headers: %r' % (headers,))

        self.finish_request(
            message['in_reply_to'],
            message['content'].encode(self.ENCODING),
            code=http.OK,
            headers=headers)
        return self.publish_ack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'])

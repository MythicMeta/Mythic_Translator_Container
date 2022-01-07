#!/usr/bin/env python3
import aio_pika
import sys
import json
import socket
import asyncio
import pathlib
from importlib import import_module, invalidate_caches
from functools import partial
from .config import settings
import logging
import time
import pika
from pika.exchange_type import ExchangeType
import threading
debug = False

LOG_FORMAT = ('%(levelname) -4s %(asctime)s %(name) -5s %(funcName) '
              '-3s %(lineno) -5d: %(message)s\n')
LOGGER = logging.getLogger(__name__)

container_version = "4"
container_pypi_version = "0.0.14"

operating_environment = "production"

def print_flush(msg, override: bool = False):
    global operating_environment
    if operating_environment in ["development", "testing", "staging"] or override:
        LOGGER.debug(msg)
        sys.stdout.flush()

def import_all_c2_functions():
    import glob
    try:
        # Get file paths of all modules.
        modules = glob.glob("c2_functions/*.py")
        invalidate_caches()
        for x in modules:
            if not x.endswith("__init__.py") and x[-3:] == ".py":
                module = import_module("c2_functions." + pathlib.Path(x).stem, package=None)
                for el in dir(module):
                    if "__" not in el:
                        globals()[el] = getattr(module, el)
    except Exception as e:
        print_flush("[-] import_all_c2_functions ran into an error: {}".format(str(e)))
        sys.exit(1)

class ExampleConsumer(object):
    """This is an example consumer that will handle unexpected interactions
    with RabbitMQ such as channel and connection closures.

    If RabbitMQ closes the connection, this class will stop and indicate
    that reconnection is necessary. You should look at the output, as
    there are limited reasons why the connection may be closed, which
    usually are tied to permission related issues or socket timeouts.

    If the channel is closed, it will indicate a problem with one of the
    commands that were issued and that should surface in the output as well.

    """
    EXCHANGE = 'mythic_traffic'
    EXCHANGE_TYPE = ExchangeType.topic
    PUBLISH_INTERVAL = 10

    def __init__(self, amqp_url, hostname: str, message_callback: callable, routing_key: str = None, is_heartbeat: bool = False):
        """Create a new instance of the consumer class, passing in the AMQP
        URL used to connect to RabbitMQ.

        :param str amqp_url: The AMQP url to connect with

        """
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.should_reconnect = False
        self.was_consuming = False
        self._hostname = hostname 
        self._message_callback = message_callback 
        self._QUEUE = f"{hostname}_rpc_queue"
        self.ROUTING_KEY = f"{hostname}_rpc_queue"
        self._connection = None
        self._channel = None
        self._closing = False
        self._consumer_tag = None
        self._url = amqp_url
        self._consuming = False
        self._is_heartbeat = is_heartbeat
        self._loop = asyncio.get_event_loop()
        if routing_key is not None:
            self.ROUTING_KEY = routing_key
        # In production, experiment with higher prefetch values
        # for higher consumer throughput
        self._prefetch_count = 1

    def connect(self):
        """This method connects to RabbitMQ, returning the connection handle.
        When the connection is established, the on_connection_open method
        will be invoked by pika.

        :rtype: pika.SelectConnection

        """
        print_flush('Connecting to %s', self._url)
        return pika.SelectConnection(
            parameters=pika.URLParameters(self._url),
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_open_error,
            on_close_callback=self.on_connection_closed)

    def close_connection(self):
        self._consuming = False
        if self._connection.is_closing or self._connection.is_closed:
            print_flush('Connection is closing or already closed')
        else:
            print_flush('Closing connection')
            self._connection.close()

    def on_connection_open(self, _unused_connection):
        """This method is called by pika once the connection to RabbitMQ has
        been established. It passes the handle to the connection object in
        case we need it, but in this case, we'll just mark it unused.

        :param pika.SelectConnection _unused_connection: The connection

        """
        print_flush('Connection opened')
        self.open_channel()

    def on_connection_open_error(self, _unused_connection, err):
        """This method is called by pika if the connection to RabbitMQ
        can't be established.

        :param pika.SelectConnection _unused_connection: The connection
        :param Exception err: The error

        """
        LOGGER.error('Connection open failed: %s', err)
        self.reconnect()

    def on_connection_closed(self, _unused_connection, reason):
        """This method is invoked by pika when the connection to RabbitMQ is
        closed unexpectedly. Since it is unexpected, we will reconnect to
        RabbitMQ if it disconnects.

        :param pika.connection.Connection connection: The closed connection obj
        :param Exception reason: exception representing reason for loss of
            connection.

        """
        self._channel = None
        if self._closing:
            self._connection.ioloop.stop()
        else:
            LOGGER.warning('Connection closed, reconnect necessary: %s', reason)
            self.reconnect()

    def reconnect(self):
        """Will be invoked if the connection can't be opened or is
        closed. Indicates that a reconnect is necessary then stops the
        ioloop.

        """
        self.should_reconnect = True
        self.stop()

    def open_channel(self):
        """Open a new channel with RabbitMQ by issuing the Channel.Open RPC
        command. When RabbitMQ responds that the channel is open, the
        on_channel_open callback will be invoked by pika.

        """
        print_flush('Creating a new channel')
        self._connection.channel(on_open_callback=self.on_channel_open)

    def on_channel_open(self, channel):
        """This method is invoked by pika when the channel has been opened.
        The channel object is passed in so we can make use of it.

        Since the channel is now open, we'll declare the exchange to use.

        :param pika.channel.Channel channel: The channel object

        """
        print_flush('Channel opened')
        self._channel = channel
        self.add_on_channel_close_callback()
        self.setup_exchange(self.EXCHANGE)

    def add_on_channel_close_callback(self):
        """This method tells pika to call the on_channel_closed method if
        RabbitMQ unexpectedly closes the channel.

        """
        print_flush('Adding channel close callback')
        self._channel.add_on_close_callback(self.on_channel_closed)

    def on_channel_closed(self, channel, reason):
        """Invoked by pika when RabbitMQ unexpectedly closes the channel.
        Channels are usually closed if you attempt to do something that
        violates the protocol, such as re-declare an exchange or queue with
        different parameters. In this case, we'll close the connection
        to shutdown the object.

        :param pika.channel.Channel: The closed channel
        :param Exception reason: why the channel was closed

        """
        LOGGER.warning('Channel %i was closed: %s', channel, reason)
        self.close_connection()

    def setup_exchange(self, exchange_name):
        """Setup the exchange on RabbitMQ by invoking the Exchange.Declare RPC
        command. When it is complete, the on_exchange_declareok method will
        be invoked by pika.

        :param str|unicode exchange_name: The name of the exchange to declare

        """
        print_flush('Declaring exchange: %s', exchange_name)
        # Note: using functools.partial is not required, it is demonstrating
        # how arbitrary data can be passed to the callback when it is called
        if self._is_heartbeat:
            cb = partial(
            self.on_exchange_declareok, userdata=exchange_name)
            self._channel.exchange_declare(
                exchange=exchange_name,
                exchange_type=self.EXCHANGE_TYPE,
                callback=cb)
        else:
            self.setup_queue(self._QUEUE)

    def on_exchange_declareok(self, _unused_frame, userdata):
        """Invoked by pika when RabbitMQ has finished the Exchange.Declare RPC
        command.

        :param pika.Frame.Method unused_frame: Exchange.DeclareOk response frame
        :param str|unicode userdata: Extra user data (exchange name)

        """
        print_flush('Exchange declared: %s', userdata)
        self.setup_queue(self._QUEUE)

    def setup_queue(self, queue_name):
        """Setup the queue on RabbitMQ by invoking the Queue.Declare RPC
        command. When it is complete, the on_queue_declareok method will
        be invoked by pika.

        :param str|unicode queue_name: The name of the queue to declare.

        """
        print_flush('Declaring queue %s', queue_name)
        cb = partial(self.on_queue_declareok, userdata=queue_name)
        self._channel.queue_declare(queue=queue_name, callback=cb, auto_delete=True)

    def on_queue_declareok(self, _unused_frame, userdata):
        """Method invoked by pika when the Queue.Declare RPC call made in
        setup_queue has completed. In this method we will bind the queue
        and exchange together with the routing key by issuing the Queue.Bind
        RPC command. When this command is complete, the on_bindok method will
        be invoked by pika.

        :param pika.frame.Method _unused_frame: The Queue.DeclareOk frame
        :param str|unicode userdata: Extra user data (queue name)

        """
        queue_name = userdata
        self.set_qos()
        #if self._is_heartbeat:
        #    LOGGER.info('Binding %s to %s with %s', self.EXCHANGE, queue_name,
        #            self.ROUTING_KEY)
        #    cb = partial(self.on_bindok, userdata=queue_name)
        #    self._channel.queue_bind(
        #        queue_name,
        #        self.EXCHANGE,
        #        routing_key=self.ROUTING_KEY,
        #        callback=cb)
        #else:
        #    self.set_qos()

    def on_bindok(self, _unused_frame, userdata):
        """Invoked by pika when the Queue.Bind method has completed. At this
        point we will set the prefetch count for the channel.

        :param pika.frame.Method _unused_frame: The Queue.BindOk response frame
        :param str|unicode userdata: Extra user data (queue name)

        """
        print_flush('Queue bound: %s', userdata)
        self.set_qos()
    
    def set_qos(self):
        """This method sets up the consumer prefetch to only be delivered
        one message at a time. The consumer must acknowledge this message
        before RabbitMQ will deliver another one. You should experiment
        with different prefetch values to achieve desired performance.

        """
        self._channel.basic_qos(
            prefetch_count=self._prefetch_count, callback=self.on_basic_qos_ok)

    def on_basic_qos_ok(self, _unused_frame):
        """Invoked by pika when the Basic.QoS method has completed. At this
        point we will start consuming messages by calling start_consuming
        which will invoke the needed RPC commands to start the process.

        :param pika.frame.Method _unused_frame: The Basic.QosOk response frame

        """
        print_flush('QOS set to: %d', self._prefetch_count)
        self.start_consuming()

    def start_consuming(self):
        """This method sets up the consumer by first calling
        add_on_cancel_callback so that the object is notified if RabbitMQ
        cancels the consumer. It then issues the Basic.Consume RPC command
        which returns the consumer tag that is used to uniquely identify the
        consumer with RabbitMQ. We keep the value to use it when we want to
        cancel consuming. The on_message method is passed in as a callback pika
        will invoke when a message is fully received.

        """
        print_flush('Issuing consumer related RPC commands')
        self.add_on_cancel_callback()
        if self._is_heartbeat:
            self.schedule_next_message()
        else:
            self._consumer_tag = self._channel.basic_consume(
                self._QUEUE, self.on_message)
            self.was_consuming = True
            self._consuming = True
            

    def on_message(self, _unused_channel, basic_deliver, properties, body):
        print_flush('Received message # %s from %s: %s', basic_deliver.delivery_tag, properties.app_id, body)
        self._loop.run_until_complete(self._message_callback(_unused_channel, basic_deliver, properties, body))

    def add_on_cancel_callback(self):
        """Add a callback that will be invoked if RabbitMQ cancels the consumer
        for some reason. If RabbitMQ does cancel the consumer,
        on_consumer_cancelled will be invoked by pika.

        """
        print_flush('Adding consumer cancellation callback')
        self._channel.add_on_cancel_callback(self.on_consumer_cancelled)

    def on_consumer_cancelled(self, method_frame):
        """Invoked by pika when RabbitMQ sends a Basic.Cancel for a consumer
        receiving messages.

        :param pika.frame.Method method_frame: The Basic.Cancel frame

        """
        print_flush('Consumer was cancelled remotely, shutting down: %r',
                    method_frame)
        if self._channel:
            self._channel.close()

    def stop_consuming(self):
        """Tell RabbitMQ that you would like to stop consuming by sending the
        Basic.Cancel RPC command.

        """
        if self._channel:
            print_flush('Sending a Basic.Cancel RPC command to RabbitMQ')
            cb = partial(
                self.on_cancelok, userdata=self._consumer_tag)
            self._channel.basic_cancel(self._consumer_tag, cb)

    def on_cancelok(self, _unused_frame, userdata):
        """This method is invoked by pika when RabbitMQ acknowledges the
        cancellation of a consumer. At this point we will close the channel.
        This will invoke the on_channel_closed method once the channel has been
        closed, which will in-turn close the connection.

        :param pika.frame.Method _unused_frame: The Basic.CancelOk frame
        :param str|unicode userdata: Extra user data (consumer tag)

        """
        self._consuming = False
        print_flush(
            'RabbitMQ acknowledged the cancellation of the consumer: %s',
            userdata)
        self.close_channel()

    def close_channel(self):
        """Call to close the channel with RabbitMQ cleanly by issuing the
        Channel.Close RPC command.

        """
        print_flush('Closing the channel')
        self._channel.close()

    def schedule_next_message(self):
        """If we are not closing our connection to RabbitMQ, schedule another
        message to be delivered in PUBLISH_INTERVAL seconds.
        """

        print_flush('Scheduling next message for %0.1f seconds',
                    self.PUBLISH_INTERVAL)
        self._connection.ioloop.call_later(self.PUBLISH_INTERVAL,
                                           self.send_heartbeat)

    def send_heartbeat(self):
        if self._channel is None or not self._channel.is_open:
            return
        properties = pika.BasicProperties(
            app_id='mythic',
            content_type='text/html')
        self._channel.basic_publish(self.EXCHANGE, self.ROUTING_KEY,
                                    "heartbeat".encode(),
                                    properties)
        print_flush('Published message')
        self.schedule_next_message()

    def run(self):
        """Run the example consumer by connecting to RabbitMQ and then
        starting the IOLoop to block and allow the SelectConnection to operate.

        """
        self._connection = self.connect()
        self._connection.ioloop.start()

    def stop(self):
        """Cleanly shutdown the connection to RabbitMQ by stopping the consumer
        with RabbitMQ. When RabbitMQ confirms the cancellation, on_cancelok
        will be invoked by pika, which will then closing the channel and
        connection. The IOLoop is started again because this method is invoked
        when CTRL-C is pressed raising a KeyboardInterrupt exception. This
        exception stops the IOLoop which needs to be running for pika to
        communicate with RabbitMQ. All of the commands issued prior to starting
        the IOLoop will be buffered but not processed.

        """
        if not self._closing:
            self._closing = True
            print_flush('Stopping')
            if self._consuming:
                self.stop_consuming()
                self._connection.ioloop.start()
            else:
                self._connection.ioloop.stop()
            print_flush('Stopped')

class ReconnectingExampleConsumer(object):
    """This is an example consumer that will reconnect if the nested
    ExampleConsumer indicates that a reconnect is necessary.

    """

    def __init__(self, amqp_url, hostname, message_callback, is_heartbeat: bool = False, routing_key: str = None):
        self._reconnect_delay = 0
        self._amqp_url = amqp_url
        self._hostname = hostname 
        self._message_callback = message_callback
        self._is_heartbeat = is_heartbeat
        self._routing_key = routing_key
        self._consumer = ExampleConsumer(self._amqp_url, hostname=hostname, message_callback=message_callback, is_heartbeat=is_heartbeat, routing_key=routing_key)
        

    def run(self):
        while True:
            try:
                self._consumer.run()
            except KeyboardInterrupt:
                self._consumer.stop()
                break
            self._maybe_reconnect()

    def _maybe_reconnect(self):
        if self._consumer.should_reconnect:
            self._consumer.stop()
            reconnect_delay = self._get_reconnect_delay()
            LOGGER.warning('Reconnecting after %d seconds', reconnect_delay)
            time.sleep(reconnect_delay)
            self._consumer = ExampleConsumer(self._amqp_url, hostname=self._hostname, message_callback=self._message_callback, is_heartbeat=self._is_heartbeat, routing_key=self._routing_key)

    def _get_reconnect_delay(self):
        if self._consumer.was_consuming:
            self._reconnect_delay = 0
        else:
            self._reconnect_delay += 1
        if self._reconnect_delay > 30:
            self._reconnect_delay = 30
        return self._reconnect_delay

async def rabbit_c2_rpc_callback(_unused_channel, basic_deliver, properties, body):
    """Invoked by pika when a message is delivered from RabbitMQ. The
    channel is passed for your convenience. The basic_deliver object that
    is passed in carries the exchange, routing key, delivery tag and
    a redelivered flag for the message. The properties passed in is an
    instance of BasicProperties with the message properties and the body
    is the message that was sent.

    :param pika.channel.Channel _unused_channel: The channel object
    :param pika.Spec.Basic.Deliver: basic_deliver method
    :param pika.Spec.BasicProperties: properties
    :param bytes body: The message body

    """
    print_flush('Received message # %s from %s: %s',
                basic_deliver.delivery_tag, properties.app_id, body)
    print_flush('Acknowledging message %s', basic_deliver.delivery_tag)
    _unused_channel.basic_ack(basic_deliver.delivery_tag)

    try:
        print_flush("got message: {}".format(body.decode()))
        request = json.loads(body.decode())
        if request["action"] == "exit_container":
            print_flush("[*] Got exit container command, exiting!")
            sys.exit(1)
        response = await globals()[request["action"]](request["message"])
        if request["action"] != "translate_to_c2_format":
            response = json.dumps(response).encode()
        print_flush("sending response: {}".format(response))
    except Exception as e:
        print_flush("[-] Error in trying to process a message from mythic: {}".format(str(e)))
        response = b""
    try:
        print_flush("sending message back to mythic")
        response_props = pika.BasicProperties(
            app_id='mythic',
            correlation_id=properties.correlation_id,
            content_type='application/json')
        _unused_channel.basic_publish("", properties.reply_to,
                                    response,
                                    response_props)
        print_flush("sent message back to mythic")
    except Exception as e:
        print_flush(
            "[-] Exception trying to send message back to container for rpc! " + str(e)
        )

def mythic_service():
    try:
        hostname = settings.get("name", "hostname")
        if hostname == "hostname":
            hostname = socket.gethostname()
            print_flush("[*] Hostname specified as default 'hostname' value, thus will fetch and use the current hostname of the Docker container or computer\n")
        print_flush("[*] Setting hostname (which should match payload type name exactly) to: " + hostname, override=True)
        print_flush("[*] got Hostname, now to import c2 functions")
        import_all_c2_functions()
        print_flush("[*] imported functions, now to start the connection loop")
        print_flush("[*] about to try to connect_robust to rabbitmq")
        host=settings.get("host", "127.0.0.1")
        login=settings.get("username", "mythic_user")
        password=settings.get("password", "mythic_password")
        virtualhost=settings.get("virtual_host", "mythic_vhost")
        port=settings.get("port", "5672")
        amqp_url = f'amqp://{login}:{password}@{host}:{port}/{virtualhost}'
        consumer = ReconnectingExampleConsumer(amqp_url, hostname, message_callback=rabbit_c2_rpc_callback)
        print_flush("[*] starting service thread")
        consumer.run()
        print_flush("[*] service thread finished")
    except Exception as f:
        print_flush("[-] Exception in main mythic_service, exiting: {}".format(str(f)), override=True)
        sys.exit(1)

def heartbeat_loop():
    try:
        hostname = settings.get("name", "hostname")
        if hostname == "hostname":
            hostname = socket.gethostname()
            print_flush("[*] Hostname specified as default 'hostname' value, thus will fetch and use the current hostname of the Docker container or computer\n")
        print_flush("[*] Setting hostname (which should match payload type name exactly) to: " + hostname, override=True)
        import_all_c2_functions()
        print_flush("[*] imported functions, now to start the connection loop")
        print_flush("[*] about to try to connect_robust to rabbitmq")
        host=settings.get("host", "127.0.0.1")
        login=settings.get("username", "mythic_user")
        password=settings.get("password", "mythic_password")
        virtualhost=settings.get("virtual_host", "mythic_vhost")
        port=settings.get("port", "5672")
        amqp_url = f'amqp://{login}:{password}@{host}:{port}/{virtualhost}'
        consumer = ReconnectingExampleConsumer(amqp_url, hostname, message_callback=rabbit_c2_rpc_callback, is_heartbeat=True, routing_key="tr.heartbeat.{}.{}".format(hostname, container_version))
        print_flush("[*] starting heartbeat thread")
        consumer.run()
        print_flush("[*] service thread finished")
    except Exception as f:
        print_flush("[-] Exception in heartbeat_loop, exiting: {}".format(str(f)), override=True)
        sys.exit(1)

def start_service_and_heartbeat():
    global operating_environment
    operating_environment = settings.get("environment", "production")
    if operating_environment == "production":
        print_flush("[*] To enable debug logging, set `MYTHIC_ENVIRONMENT` variable to `testing`", override=True)
    # start our service
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    get_version_info()
    print_flush("[*] Entered start_service")
    loop = asyncio.get_event_loop()
    print_flush("[*] created task for mythic_service and heartbeat")
    thread1 = threading.Thread(target=mythic_service)
    thread2 = threading.Thread(target=heartbeat_loop)
    thread1.start()
    thread2.start()
    loop.run_forever()
    thread1.join()
    thread2.join()
    print_flush("[*] finished loop.run_forever()")

def get_version_info():
    print_flush("[*] Mythic Translator Version: " + container_version, override=True)
    print_flush("[*] Mythic Translator PyPi version: " + container_pypi_version, override=True)

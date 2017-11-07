import asyncio
import json
import logging
import websockets

from aiohttp import ClientSession
from http import HTTPStatus
from .discord_bot_exception import DiscordBotException
from .events import Events
from .opcodes import Opcodes


CONFIG_FILE_PATH = "configs/config.json"


class DiscordBot:
    """
    Establishes a connection to the discord gateway and handles varied messages
    """

    def __init__(self):
        # instance variables
        self.config = None
        self.gateway_ws_url = None
        self.heartbeat_interval_ms = None
        self.last_seq = None
        self.logger = None
        self.event_loop = None
        self.websocket = None
        self.session_id = None

    def setup(self):
        """
        Begins the discord bot by loading the configurations, sets up the logger for debugging purposes,
        and retrieves the gateway URL.
        """
        self.config = self.load_config(CONFIG_FILE_PATH)

        logging.basicConfig(format='%(asctime)s %(module)s %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S %p')
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(self.config["log_level"])

        self.event_loop = asyncio.get_event_loop()
        self.gateway_ws_url = self.event_loop.run_until_complete(self.get_gateway())

    def run(self):
        """
        Establishes connectivity to the gateway by sending heartbeat payloads and an identify payload. Also, this will
        perform various functions such as responding to specific commands.
        """
        self.event_loop.run_until_complete(self.gateway_handler())
        self.event_loop.close()

    @staticmethod
    def load_config(config_file_path):
        """
        Loads the configurations
        :return: dict - the configurations
        """
        with open(config_file_path) as f:
            return json.load(f)

    async def get_gateway(self):
        """
        Caches a gateway value, authenticates, and retrieves a new URL
        :return: gateway URL
        """
        # TODO: configurable timeout for all of our REST requests
        async with ClientSession() as session:
            async with session.request("GET", "{}/gateway".format(self.config["discord_api_endpoint"])) as r:
                if r.status == HTTPStatus.OK:
                    json_response = await r.json()
                    return json_response["url"]
                else:
                    raise DiscordBotException(
                        f"Expected gateway URL, received HTTP status code: {r.status} instead of 200, aborting program."
                    )

    async def send_json(self, payload):
        """
        Sends a payload to the websocket as JSON string.
        """
        asyncio.ensure_future(self.websocket.send(json.dumps(payload)))

    async def handshake(self):
        """
        When a websocket connection is opened, Hello payload is received. Then, an Identify/Resume payload is sent as
        part of the handshake to authorize this client.
        """
        handshake_identity = self.config["handshake_identity"]
        if self.session_id:
            payload = {
                "op": Opcodes.RESUME,
                "d": {
                    "token": self.config["handshake_identity"]["token"],
                    "session_id": self.session_id,
                    "seq": self.last_seq
                }
            }
            asyncio.ensure_future(self.send_json(payload))
        else:
            payload = {
                "op": Opcodes.IDENTIFY,
                "d": handshake_identity
            }
            asyncio.ensure_future(self.send_json(payload))

    async def heartbeat(self):
        """
        Sends a heartbeat payload every heartbeat interval.
        """
        await asyncio.sleep(self.heartbeat_interval_ms / 1000.0)
        self.logger.info("Last Sequence: {}".format(self.last_seq))
        payload = {
            "op": Opcodes.HEARTBEAT,
            "d": self.last_seq
        }
        asyncio.ensure_future(self.send_json(payload))
        asyncio.ensure_future(self.heartbeat())

    async def hello_handler(self, message):
        """
        Does the following methods when the Opcode is "HELLO"
        :param message: Hello message
        """
        self.heartbeat_interval_ms = message["d"]["heartbeat_interval"]
        asyncio.ensure_future(self.heartbeat())
        asyncio.ensure_future(self.handshake())

    async def gateway_handler(self):
        """
        Creates the websocket, receives responses and acts on them.
        """
        async with websockets.connect("{}/?v={}&encoding={}".format(self.gateway_ws_url,
                                                                    self.config["gateway_api_version"],
                                                                    self.config["gateway_encoding"])) as websocket:
            self.websocket = websocket
            while True:
                message = await self.websocket.recv()
                message = json.loads(message)
                self.logger.info("{}: {}".format(Opcodes(message["op"]).name, message))
                if message["s"] is not None:
                    self.last_seq = message["s"]

                if message["op"] == Opcodes.HELLO:
                    asyncio.ensure_future(self.hello_handler(message))
                elif message["op"] == Opcodes.HEARTBEAT_ACK:
                    pass
                elif message["op"] == Opcodes.INVALID_SESSION:
                    self.logger.warning("Invalid Session")
                elif message["op"] == Opcodes.DISPATCH:
                    event = message["t"]
                    if event == Events.READY:
                        self.session_id = message["d"]["session_id"]
                    elif event == Events.MESSAGE_CREATE:
                        await self.respond_message(message)
                else:
                    self.logger.exception("Unexpected opcode {}: {}".format(message["op"], message))

    async def respond_message(self, message):
        """
        Receives a message, parses the message, and responds to the user in the same channel.
        :param message: obj - the payload of the MESSAGE_CREATE event
        :return: string - defined by data
        """
        channel_id = message["d"]["channel_id"]
        author_id = message["d"]["author"]["id"]
        content = message["d"]["content"]
        if author_id != self.config["momoi_id"]:
            if content.startswith("!help"):
                async with ClientSession() as session:
                    async with session.request(
                            "POST",
                            "{}/channels/{}/messages".format(self.config["discord_api_endpoint"], channel_id),
                            headers={
                                "Authorization": "Bot {}".format(self.config["handshake_identity"]["token"]),
                                "User-Agent": "{}".format(self.config["user_agent"])
                            },
                            data={
                                "content": f"<@{author_id}> how may I help?"
                            }) as r:
                        if r.status == HTTPStatus.OK:
                            return await r.json()
                        else:
                            raise DiscordBotException(
                                f"Expected a post to discord channel {channel_id},"
                                f" received HTTP status code: {r.status} instead of 200, aborting program."
                            )

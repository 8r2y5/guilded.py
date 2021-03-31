import asyncio
import concurrent.futures
import datetime
import json
import logging
import sys
import threading
import traceback

import aiohttp

from . import GuildedException
from .message import Message
from .user import Member

log = logging.getLogger(__name__)


class WebSocketClosure(Exception):
    """An exception to make up for the fact that aiohttp doesn't signal closure."""

    pass


class GuildedWebSocket:
    """Implements Guilded's global gateway as well as team websocket connections."""

    HEARTBEAT_PAYLOAD = "2"

    def __init__(self, socket, client, *, loop):
        self.socket = socket
        self.client = client
        self.loop = loop
        self._heartbeater = None

        # ws
        self._close_code = None

        # actual gateway garbage
        self.sid = None
        self.upgrades = []

        # I'm aware of the python-engineio package but
        # have opted not to use it so as to have less
        # dependencies, and thus fewer links in the chain.

    async def send(self, payload):
        return await self.socket.send_str(payload)

    @property
    def latency(self):
        return (
            float("inf")
            if self._heartbeater is None
            else self._heartbeater.latency
        )

    @classmethod
    async def build(cls, client, *, loop=None, **gateway_args):
        log.info(f"Connecting to the gateway with args {gateway_args}")
        try:
            socket = await client.http.ws_connect(**gateway_args)
        except aiohttp.client_exceptions.WSServerHandshakeError as exc:
            log.error(f"Failed to connect: {exc}")
            return exc
        else:
            log.info("Connected")

        ws = cls(socket, client, loop=loop or asyncio.get_event_loop())
        ws._parsers = WebSocketEventParsers(client)
        await ws.send(GuildedWebSocket.HEARTBEAT_PAYLOAD)
        await ws.poll_event()

        return ws

    def _pretty_event(self, payload):
        if type(payload) == list:
            payload = payload[1]
        if not payload.get("type"):
            return payload

        return {
            "type": payload.pop("type"),
            "data": {k: v for k, v in payload.items()},
        }

    def _full_event_parse(self, payload):
        for char in payload:
            if char.isdigit():
                payload = payload.replace(char, "", 1)
            else:
                break
        data = json.loads(payload)
        return self._pretty_event(data)

    async def received_event(self, payload):
        if payload.isdigit():
            return

        self.client.dispatch("socket_raw_receive", payload)
        data = self._full_event_parse(payload)
        self.client.dispatch("socket_response", data)
        log.debug(f"Received {data}")

        if data.get("sid"):
            # hello
            self.sid = data["sid"]
            self.upgrades = data["upgrades"]
            self._heartbeater = Heartbeater(
                ws=self, interval=data["pingInterval"] / 1000
            )  # , timeout=60.0) # data['pingTimeout']
            await self.send(self.HEARTBEAT_PAYLOAD)
            self._heartbeater.start()
            return

        event = self._parsers.get(data["type"], data["data"])
        if event is None:
            # ignore unhandled events
            return
        try:
            await event
        except Exception as e:
            if isinstance(e, GuildedException):
                self.client.dispatch("error", e)
                raise e
            else:
                # wrap error if not already from the lib
                exc = GuildedException(e)
                self.client.dispatch("error", exc)
                raise exc from e

    async def poll_event(self):
        msg = await self.socket.receive()
        if msg.type is aiohttp.WSMsgType.TEXT:
            await self.received_event(msg.data)
        elif msg.type is aiohttp.WSMsgType.ERROR:
            raise msg.data
        elif msg.type in (
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSE,
        ):
            raise WebSocketClosure("Socket is in a closed or closing state.")
        return None

    async def close(self, code=1000):
        self._close_code = code
        await self.socket.close(code=code)


class WebSocketEventParsers:
    def __init__(self, client):
        self.client = client
        self._state = client.http

    def get(self, event_name, data):
        coro = getattr(self, event_name, None)
        if not coro:
            return None
        return coro(data)

    async def ChatMessageCreated(self, data):
        channelId = data.get(
            "channelId", data.get("message", {}).get("channelId")
        )
        createdBy = data.get(
            "createdBy", data.get("message", {}).get("createdBy")
        )
        teamId = data.get("teamId", data.get("message", {}).get("teamId"))
        channel, author, team = None, None, None

        if channelId:
            try:
                channel = await self.client.getch_channel(channelId)
            except:
                channel = None

        if createdBy:
            if channel:
                try:
                    author = await channel.team.getch_member(createdBy)
                except:
                    author = None
            else:
                try:
                    author = await self.client.getch_user(createdBy)
                except:
                    author = None

        if teamId:
            if channel:
                team = channel.team
            else:
                try:
                    team = await self.client.getch_team(teamId)
                except:
                    team = None

        message = Message(
            state=self.client.http,
            channel=channel,
            data=data,
            author=author,
            team=team,
        )
        self._state.add_to_message_cache(message)
        self.client.dispatch("message", message)

    async def ChatChannelTyping(self, data):
        self.client.dispatch(
            "typing",
            data["channelId"],
            data["userId"],
            datetime.datetime.utcnow(),
        )

    async def ChatMessageDeleted(self, data):
        message = self.client.get_message(data["message"]["id"])
        data["cached_message"] = message
        self.client.dispatch("raw_message_delete", data)
        if message is not None:
            try:
                self.client.cached_messages.remove(message)
            except:
                pass
            finally:
                self.client.dispatch("message_delete", message)

    async def ChatPinnedMessageCreated(self, data):
        if data.get("channelType") == "Team":
            self.client.dispatch("raw_team_message_pinned", data)
        else:
            self.client.dispatch("raw_dm_message_pinned", data)
        message = self.client.get_message(data["message"]["id"])
        if message is None:
            return

        if message.team is not None:
            self.client.dispatch("team_message_pinned", message)
        else:
            self.client.dispatch("dm_message_pinned", message)

    async def ChatPinnedMessageDeleted(self, data):
        if data.get("channelType") == "Team":
            self.client.dispatch("raw_team_message_unpinned", data)
        else:
            self.client.dispatch("raw_dm_message_unpinned", data)
        message = self.client.get_message(data["message"]["id"])
        if message is None:
            return  # message = PartialMessage()

        if message.team is not None:
            self.client.dispatch("team_message_unpinned", message)
        else:
            self.client.dispatch("dm_message_unpinned", message)

    async def ChatMessageUpdated(self, data):
        self.client.dispatch("raw_message_edit", data)
        before = self.client.get_message(data["message"]["id"])
        if before is None:
            return

        data["webhookId"] = before.webhook_id
        data["createdAt"] = before._raw.get("createdAt")

        after = Message(
            state=self.client.http,
            channel=before.channel,
            author=before.author,
            data=data,
        )
        self._state.add_to_message_cache(after)

        self.client.dispatch("message_edit", before, after)

    async def TeamXpSet(self, data):
        if not data.get("amount"):
            return
        team = self.client.get_team(data["teamId"])
        if team is None:
            return
        before = team.get_member(data["userIds"][0])
        if before is None:
            return

        after = team.get_member(before.id)
        after.xp = data["amount"]
        self._state.add_to_member_cache(after)
        self.client.dispatch("member_update", before, after)

    async def TeamMemberUpdated(self, data):
        raw_after = Member(state=self.client.http, data=data)
        self.client.dispatch("raw_member_update", raw_after)

        team = self.client.get_team(data["teamId"])
        if team is None:
            return
        before = team.get_member(data["userId"])
        if before is None:
            return

        for key, val in data["userInfo"].items():
            after = team.get_member(data["userId"])
            setattr(after, key, val)
            self._state.add_to_member_cache(after)

        self.client.dispatch("member_update", before, after)

    async def teamRolesUpdates(self, data):
        # yes, this event name is camelcased
        team = self.client.get_team(data["teamId"])
        if team is None:
            return

        befores_afters = []
        for updated in data["memberRoleIds"]:
            before = team.get_member(updated["userId"])
            if not before:
                continue

            after = team.get_member(before.id)
            after.roles = updated["roleIds"]
            self._state.add_to_member_cache(after)
            befores_afters.append([before, after])

        for b, a in befores_afters:
            self.client.dispatch("member_update", b, a)


class Heartbeater(threading.Thread):
    def __init__(self, ws, *, interval):
        self.ws = ws
        self.interval = interval
        # self.heartbeat_timeout = timeout
        threading.Thread.__init__(self)

        self.msg = "Keeping websocket alive with sequence %s."
        self.block_msg = (
            "Websocket heartbeat blocked for more than %s seconds."
        )
        self.behind_msg = "Can't keep up, websocket is %.1fs behind."
        self._stop_ev = threading.Event()
        self.latency = float("inf")

    def run(self):
        log.debug("Started heartbeat thread")
        while not self._stop_ev.wait(self.interval):
            log.debug("Sending heartbeat")
            coro = self.ws.send(GuildedWebSocket.HEARTBEAT_PAYLOAD)
            f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
            try:
                total = 0
                while True:
                    try:
                        f.result(10)
                        break
                    except concurrent.futures.TimeoutError:
                        total += 10
                        try:
                            frame = sys._current_frames()[self._main_thread_id]
                        except KeyError:
                            msg = self.block_msg
                        else:
                            stack = traceback.format_stack(frame)
                            msg = (
                                "%s\nLoop thread traceback (most recent call last):\n%s"
                                % (self.block_msg, "".join(stack))
                            )
                        log.warning(msg, total)

            except Exception:
                self.stop()

    def stop(self):
        self._stop_ev.set()

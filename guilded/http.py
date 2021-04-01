import asyncio
import datetime
import json
import logging
from typing import Union

from guilded import utils
from guilded.errors import GuildedException, HTTPException, error_mapping

log = logging.getLogger(__name__)


class Route:
    BASE = "https://www.guilded.gg/api"
    MEDIA_BASE = "https://media.guilded.gg"
    CDN_BASE = "https://s3-us-west-2.amazonaws.com/www.guilded.gg"
    NO_BASE = ""

    def __init__(self, method, path, *, override_base=None):
        self.method = method
        self.path = path

        if override_base is not None:
            self.BASE = override_base

        self.url = self.BASE + path


class HTTPClient:
    def __init__(self, *, session, max_messages=1000):
        self.session = session
        self.ws = None
        self.my_id = None

        self.email = None
        self.password = None
        self.cookies = None

        self._max_messages = max_messages
        self._users = {}
        self._teams = {}
        self._emojis = {}
        self._messages = {}
        self._team_members = {}
        self._team_channels = {}
        self._threads = {}
        self._dm_channels = {}

    def _get_user(self, id):
        return self._users.get(id)

    def _get_team(self, id):
        return self._teams.get(id)

    def _get_message(self, id):
        return self._messages.get(id)

    def _get_team_channel(self, id):
        return self._team_channels.get(id)

    def _get_dm_channel(self, id):
        return self._dm_channels.get(id)

    def _get_team_member(self, team_id, id):
        return self._team_members.get(team_id, {}).get(id)

    def add_to_message_cache(self, message):
        self._messages[message.id] = message
        while len(self._messages) > self._max_messages:
            del self._messages[list(self._messages.keys())[0]]

    def add_to_team_cache(self, team):
        self._teams[team.id] = team

    def add_to_member_cache(self, member):
        self._team_members[member.team.id] = self._team_members.get(
            member.team.id, {}
        )
        self._team_members[member.team.id][member.id] = member

    def add_to_team_channel_cache(self, channel):
        self._team_channels[channel.id] = channel

    def add_to_dm_channel_cache(self, channel):
        self._dm_channels[channel.id] = channel

    @property
    def credentials(self):
        return {"email": self.email, "password": self.password}

    async def request(self, route, **kwargs):
        url = route.url
        method = route.method

        async def perform():
            log_data = f'with {kwargs["json"]}' if kwargs.get("json") else ""
            log.info(f"{method} {route.path} {log_data}")
            response = await self.session.request(method, url, **kwargs)
            log.info(f"Guilded responded with HTTP {response.status}")
            if response.status == 204:
                return None

            data = await response.json()
            log.debug(f"Guilded responded with {data}")
            if response.status != 200:

                if response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    log.warning(
                        f"Rate limited on {route.path}. Retrying in {retry_after or 5} seconds"
                    )
                    if retry_after:
                        await asyncio.sleep(retry_after)
                        data = await perform()
                    else:
                        await asyncio.sleep(5)
                        data = await perform()
                        # raise TooManyRequests(response)

                elif response.status >= 400:
                    raise error_mapping.get(response.status, HTTPException)(
                        response
                    )

            return data if route.path != "/login" else response

        return await perform()

    async def login(self, email, password):
        self.email = email
        self.password = password
        response = await self.request(
            Route("POST", "/login"), json=self.credentials
        )
        self.cookies = response.headers.get("Set-Cookie")
        data = await self.request(Route("GET", "/me"))
        return data

    async def logout(self):
        return await self.request(Route("POST", "/logout"))

    async def ws_connect(self, cookies=None, **gateway_args):
        cookies = cookies or self.cookies
        if not cookies:
            raise GuildedException(
                "No authentication cookies available. Get these from "
                "logging into the REST API at least once "
                "on this Client."
            )
        gateway_args = {
            **gateway_args,
            "jwt": "undefined",
            "EIO": "3",
            "transport": "websocket",
        }

        return await self.session.ws_connect(
            "wss://api.guilded.gg/socket.io/?{}".format(
                "&".join([f"{key}={val}" for key, val in gateway_args.items()])
            ),
            headers={"cookie": cookies},
        )

    def send_message(
        self, channel_id: str, *, content=None, embeds=[], files=[]
    ):
        route = Route("POST", f"/channels/{channel_id}/messages")
        payload = {
            "messageId": utils.new_uuid(),
            "content": {
                "object": "value",
                "document": {"object": "document", "data": {}, "nodes": []},
            },
        }

        if content:
            payload["content"]["document"]["nodes"].append(
                {
                    "object": "block",
                    "type": "markdown-plain-text",
                    "data": {},
                    "nodes": [
                        {
                            "object": "text",
                            "leaves": [
                                {
                                    "object": "leaf",
                                    "text": str(content),
                                    "marks": [],
                                }
                            ],
                        }
                    ],
                }
            )

        if embeds:
            payload["content"]["document"]["nodes"].append(
                {
                    "object": "block",
                    "type": "webhookMessage",
                    "data": {"embeds": embeds},
                    "nodes": [],
                }
            )

        if files:
            for file in files:
                payload["content"]["document"]["nodes"].append(
                    {
                        "object": "block",
                        "type": file.file_type,
                        "data": {"src": file.url},
                        "nodes": [],
                    }
                )

        return self.request(route, json=payload)

    def edit_message(self, channel_id: str, message_id: str, **fields):
        route = Route("PUT", f"/channels/{channel_id}/messages/{message_id}")
        payload = {
            "content": {
                "object": "value",
                "document": {"object": "document", "data": {}, "nodes": []},
            }
        }

        try:
            content = fields["content"]
        except KeyError:
            if fields.get("old_content"):
                content = fields.get("old_content")
                payload["content"]["document"]["nodes"].append(
                    {
                        "object": "block",
                        "type": "markdown-plain-text",
                        "data": {},
                        "nodes": [
                            {
                                "object": "text",
                                "leaves": [
                                    {
                                        "object": "leaf",
                                        "text": str(content),
                                        "marks": [],
                                    }
                                ],
                            }
                        ],
                    }
                )
        else:
            payload["content"]["document"]["nodes"].append(
                {
                    "object": "block",
                    "type": "markdown-plain-text",
                    "data": {},
                    "nodes": [
                        {
                            "object": "text",
                            "leaves": [
                                {
                                    "object": "leaf",
                                    "text": str(content),
                                    "marks": [],
                                }
                            ],
                        }
                    ],
                }
            )

        try:
            embeds = fields["embeds"]
        except KeyError:
            if fields.get("old_embeds"):
                embeds = fields.get("old_embeds")
                payload["content"]["document"]["nodes"].append(
                    {
                        "object": "block",
                        "type": "webhookMessage",
                        "data": {"embeds": embeds},
                        "nodes": [],
                    }
                )
        else:
            payload["content"]["document"]["nodes"].append(
                {
                    "object": "block",
                    "type": "webhookMessage",
                    "data": {"embeds": embeds},
                    "nodes": [],
                }
            )

        try:
            files = fields["files"]
        except KeyError:
            if fields.get("old_files"):
                files = fields.get("old_files")
                for file in files:
                    payload["content"]["document"]["nodes"].append(
                        {
                            "object": "block",
                            "type": file.file_type,
                            "data": {"src": file.url},
                            "nodes": [],
                        }
                    )
        else:
            for file in files:
                payload["content"]["document"]["nodes"].append(
                    {
                        "object": "block",
                        "type": file.file_type,
                        "data": {"src": file.url},
                        "nodes": [],
                    }
                )

        return self.request(route, json=payload)

    def delete_message(self, channel_id: str, message_id: str):
        return self.request(
            Route("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        )

    def trigger_typing(self, channel_id):
        payload = ["ChatChannelTyping", {"channelId": channel_id}]
        return self.ws.send(f"42{json.dumps(payload)}")

    def search_teams(self, query):
        return self.request(
            Route("GET", "/search"),
            params={"query": query, "entityType": "team"},
        )

    def join_team(self, team_id):
        return self.request(
            Route("PUT", f"/teams/{team_id}/members/{self.my_id}/join")
        )

    def leave_team(self, team_id):
        return self.request(
            Route("DELETE", f"/teams/{team_id}/members/{self.my_id}")
        )

    def accept_invite(self, invite_code):
        return self.request(
            Route("PUT", f"/invites/{invite_code}"), json={"type": "consume"}
        )

    def create_team_invite(self, team_id):
        return self.request(
            Route("POST", f"/teams/{team_id}/invites"),
            json={"teamId": team_id},
        )

    def update_activity(
        self, activity, *, expires: Union[int, datetime.datetime] = 0
    ):
        payload = {
            "content": {
                "document": {"object": "document", "data": [], "nodes": []}
            }
        }
        payload["content"]["document"]["nodes"].append(
            {
                "object": "text",
                "leaves": [
                    {"object": "leaf", "text": activity.details, "marks": []}
                ],
            }
        )
        if activity.emoji:
            payload["customReactionId"] = activity.emoji.id
            payload["customReaction"] = activity.emoji._raw
        if type(expires) == datetime.datetime:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            expires = (expires - now).total_seconds()

        payload["expireInMs"] = expires * 1000

        return self.request(Route("POST", "/users/me/status"), json=payload)

    def delete_team_emoji(self, team_id: str, emoji_id: int):
        return self.request(
            Route("DELETE", f"/teams/{team_id}/emoji/{emoji_id}")
        )

    def upload_file(self, file):
        return self.request(
            Route("POST", "/media/upload", override_base=Route.MEDIA_BASE),
            data={"file": file._bytes},
            params={"dynamicMediaTypeId": str(file.type)},
        )

    def get_team_members(self, team_id: str):
        return self.request(Route("GET", f"/teams/{team_id}/members"))

    def get_team_member(self, team_id: str, user_id: str):
        return self.request(
            Route("GET", f"/teams/{team_id}/members/{user_id}")
        )

    def get_team_channels(self, team_id: str):
        return self.request(Route("GET", f"/teams/{team_id}/channels"))

    def change_team_member_nickname(
        self, team_id: str, user_id: str, nickname: str
    ):
        return self.request(
            Route("GET", f"/teams/{team_id}/members/{user_id}/nickname"),
            json={"nickname": nickname},
        )

    def reset_team_member_nickname(self, team_id: str, user_id: str):
        return self.request(
            Route("DELETE", f"/teams/{team_id}/members/{user_id}/nickname")
        )

    def create_team_group(
        self,
        team_id: str,
        *,
        name: str,
        description: str,
        icon_url: str = None,
        game_id: int = None,
        membership_role_id: int = None,
        additional_membership_role_ids: list = [],
        emoji_id: int = None,
        public: bool = True,
        base: bool = False,
        users: list = [],
    ):
        return self.request(
            Route("POST", f"/teams{team_id}/groups"),
            json={
                "name": name,
                "description": description,
                "avatar": icon_url,
                "gameId": game_id,
                "membershipTeamRoleId": membership_role_id,
                "additionalMembershipTeamRoleIds": additional_membership_role_ids,
                "customReactionId": emoji_id,
                "isPublic": public,
                "isBase": base,
                "users": users,
            },
        )

    def update_team_group(
        self,
        team_id: str,
        group_id: str,
        *,
        name: str,
        description: str,
        icon_url: str = None,
        game_id: int = None,
        membership_role_id: int = None,
        additional_membership_role_ids: list = [],
        emoji_id: int = None,
        public: bool = True,
        base: bool = False,
        users: list = [],
    ):
        return self.request(
            Route("PUT", f"/teams{team_id}/groups/{group_id}"),
            json={
                "name": name,
                "description": description,
                "avatar": icon_url,
                "gameId": game_id,
                "membershipTeamRoleId": membership_role_id,
                "additionalMembershipTeamRoleIds": additional_membership_role_ids,
                "customReactionId": emoji_id,
                "isPublic": public,
                "isBase": base,
                "users": users,
            },
        )

    def delete_team_group(self, team_id: str, group_id: str):
        return self.request(
            Route("DELETE", f"/teams/{team_id}/groups/{group_id}")
        )

    def delete_team_channel(
        self, team_id: str, group_id: str, channel_id: str
    ):
        return self.request(
            Route(
                "DELETE",
                f'/teams/{team_id}/groups/{group_id or "undefined"}/channels/{channel_id}',
            )
        )

    def get_channel_messages(self, channel_id: str, *, limit: int):
        return self.request(
            Route("GET", f"/channels/{channel_id}/messages"),
            params={"limit": limit},
        )

    def get_channel_message(self, channel_id: str, message_id: str):
        return self.request(
            Route(
                "GET",
                f"/content/route/metadata?route=//channels/{channel_id}/chat?messageId={message_id}",
            )
        )

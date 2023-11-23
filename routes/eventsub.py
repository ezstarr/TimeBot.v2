"""Copyright 2023 TimeEnjoyed <https://github.com/TimeEnjoyed/>

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from starlette.responses import Response

import core
from api import View, route


if TYPE_CHECKING:
    from starlette.requests import Request

    from api import Server
    from types_.eventsub import EventSubHeaders


logger: logging.Logger = logging.getLogger(__name__)


SECRET: str = core.config["TWITCH"]["eventsub_secret"]
MESSAGE_TYPES = ["notification", "webhook_callback_verification", "revocation"]
USER_ROLES: dict[str, int] = {
    core.config["TIME_SUBS"]["twitch_id"]: core.config["TIME_SUBS"]["online_role_id"],
    core.config["BUNNIE_SUBS"]["twitch_id"]: core.config["BUNNIE_SUBS"]["online_role_id"],
    core.config["FAFFIN_SUBS"]["twitch_id"]: core.config["FAFFIN_SUBS"]["online_role_id"],
}


class EventSub(View):
    def __init__(self, app: Server) -> None:
        self.app = app

        self.responded: list[str] = []

    def verify_message(self, *, headers: EventSubHeaders, body: bytes) -> None:
        msg_id: str = headers["Twitch-Eventsub-Message-Id"]
        if msg_id in self.responded:
            raise ValueError("Already responded to this message ID.")

        timestamp: str = headers["Twitch-Eventsub-Message-Timestamp"]
        signature: str = headers["Twitch-Eventsub-Message-Signature"]

        hmac_payload: bytes = f"{msg_id}{timestamp}{body.decode('utf-8')}".encode()
        secret: bytes = SECRET.encode("utf-8")

        hmac_: hmac.HMAC = hmac.new(secret, digestmod=hashlib.sha256)
        hmac_.update(hmac_payload)

        if not hmac.compare_digest(hmac_.hexdigest(), signature[7:]):
            logger.warning("Unknown EventSub Signature.")
            raise ValueError("Unknown EventSub Signature.")

    @route("/callback", methods=["POST"])
    async def callback(self, request: Request) -> Response:
        headers: EventSubHeaders = request.headers  # type: ignore
        body: bytes = await request.body()

        message_type: str = headers.get("Twitch-Eventsub-Message-Type", "")
        if not message_type or message_type not in MESSAGE_TYPES:
            return Response("Unknown EventSub Message Type.", status_code=400)

        try:
            self.verify_message(headers=headers, body=body)
        except ValueError:
            return Response("Unable to verify EventSub integrity.", status_code=400)

        self.responded.append(headers["Twitch-Eventsub-Message-Id"])
        data: dict[str, Any] = json.loads(body)

        if message_type == "webhook_callback_verification":
            return Response(data["challenge"], status_code=200, headers={"Content-Type": "text/plain"})

        if message_type == "revocation":
            logger.warning(
                "EventSub subscription revoked %s. Reason: %s",
                data["subscription"]["type"],
                data["subscription"]["status"],
            )
            return Response(status_code=204)

        if message_type == "notification":
            _: asyncio.Task = asyncio.create_task(self.notifcation_event(data))

        return Response(status_code=204)

    async def notifcation_event(self, data: dict[str, Any], /) -> None:
        subscription: str = data["type"]

        if subscription == "stream.online":
            await self.online_event(data["broadcaster_user_login"], data["broadcaster_user_id"])

        elif subscription == "channel.channel_points_custom_reward_redemption.add":
            await self.redeem_event(data)

        else:
            logger.warning("EventSub received an unknown notification type: %s", subscription)

    async def online_event(self, stream: str, stream_id: str) -> None:
        async with aiohttp.ClientSession() as session:
            webhook: discord.Webhook = discord.Webhook.from_url(
                url=core.config["GENERAL"]["announcements_webhook"], session=session
            )

            mention: int = USER_ROLES[stream_id]
            await webhook.send(f"<@&{mention}> - **{stream}** is live: [Watch](https://twitch.tv/{stream})")

    async def redeem_event(self, data: dict[str, Any]) -> None:
        event: dict[str, Any] = data["event"]

        if event["status"].lower() != "unfulfilled":
            return

        reward: dict[str, Any] = event["reward"]
        if reward["title"].lower() != "play this song":
            return

        self.app.tbot.run_event("api_request_song", event)
        logger.info("EventSub dispatched event for <Play this song> to <api_request_song>.")
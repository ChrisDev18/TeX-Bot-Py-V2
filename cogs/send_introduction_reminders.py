"""Contains cog classes for any send_introduction_reminders interactions."""

import datetime
import logging

import discord
import emoji
from discord import ui
from discord.ext import tasks
from discord.ui import View
from django.core.exceptions import ValidationError

from cogs._utils import TeXBotCog
from config import settings
from db.core.models import (
    IntroductionReminderOptOutMember,
    SentOneOffIntroductionReminderMember,
)
from exceptions import GuestRoleDoesNotExist, GuildDoesNotExist
from utils import TeXBot


class SendIntroductionRemindersTaskCog(TeXBotCog):
    """Cog class that defines the send_introduction_reminders task."""

    def __init__(self, bot: TeXBot) -> None:
        """Start all task managers when this cog is initialised."""
        if settings["SEND_INTRODUCTION_REMINDERS"]:
            if settings["SEND_INTRODUCTION_REMINDERS"] == "interval":
                SentOneOffIntroductionReminderMember.objects.all().delete()

            self.send_introduction_reminders.start()

        super().__init__(bot)

    def cog_unload(self) -> None:
        """
        Unload hook that ends all running tasks whenever the tasks cog is unloaded.

        This may be run dynamically or when the bot closes.
        """
        self.send_introduction_reminders.cancel()

    @TeXBotCog.listener()
    async def on_ready(self) -> None:
        """Add OptOutIntroductionRemindersView to the bot's list of permanent views."""
        self.bot.add_view(
            self.OptOutIntroductionRemindersView(self.bot)
        )

    @tasks.loop(**settings["INTRODUCTION_REMINDER_INTERVAL"])
    async def send_introduction_reminders(self) -> None:
        """
        Recurring task to send an introduction reminder message to Discord members' DMs.

        The introduction reminder suggests that the Discord member should send a message to
        introduce themselves to the CSS Discord server.

        See README.md for the full list of conditions for when these
        reminders are sent.
        """
        try:
            guild: discord.Guild = self.bot.css_guild
        except GuildDoesNotExist as guild_error:
            logging.critical(guild_error)
            await self.bot.close()
            return

        guest_role: discord.Role | None = await self.bot.guest_role
        if not guest_role:
            logging.critical(GuestRoleDoesNotExist())
            await self.bot.close()
            return

        member: discord.Member
        for member in guild.members:
            if guest_role in member.roles or member.bot:
                continue

            if not member.joined_at:
                logging.error(
                    (
                        "Member with ID: %s could not be checked whether to send"
                        " introduction_reminder, because their \"joined_at\" attribute"
                        " was None."
                    ),
                    member.id
                )
                continue

            member_needs_one_off_reminder: bool = (
                settings["SEND_INTRODUCTION_REMINDERS"] == "once"
                and not await SentOneOffIntroductionReminderMember.objects.filter(
                    hashed_member_id=SentOneOffIntroductionReminderMember.hash_member_id(
                        member.id
                    )
                ).aexists()
            )
            member_needs_recurring_reminder: bool = (
                    settings["SEND_INTRODUCTION_REMINDERS"] == "interval"
            )
            member_recently_joined: bool = (discord.utils.utcnow() - member.joined_at) <= max(
                settings["KICK_NO_INTRODUCTION_MEMBERS_DELAY"] / 3,
                datetime.timedelta(days=1)
            )
            member_opted_out_from_reminders: bool = await IntroductionReminderOptOutMember.objects.filter(  # noqa: E501
                hashed_member_id=IntroductionReminderOptOutMember.hash_member_id(member.id)
            ).aexists()
            member_needs_reminder: bool = (
                    (member_needs_one_off_reminder or member_needs_recurring_reminder)
                    and not member_recently_joined
                    and not member_opted_out_from_reminders
            )

            if member_needs_reminder:
                async for message in member.history():
                    # noinspection PyUnresolvedReferences
                    message_contains_opt_in_out_button: bool = (
                        bool(message.components)
                        and isinstance(message.components[0], discord.ActionRow)
                        and isinstance(message.components[0].children[0], discord.Button)
                        and message.components[0].children[0].custom_id == "opt_out_introduction_reminders_button"  # noqa: E501
                    )
                    if message_contains_opt_in_out_button:
                        await message.edit(view=None)

                await member.send(
                    content=(
                        "Hey! It seems like you joined the CSS Discord server"
                        " but have not yet introduced yourself.\nYou will only get access"
                        " to the rest of the server after sending"
                        " an introduction message."
                    ),
                    view=(
                        self.OptOutIntroductionRemindersView(self.bot)
                        if settings["SEND_INTRODUCTION_REMINDERS"] == "interval"
                        else None  # type: ignore[arg-type]
                    )
                )

                await SentOneOffIntroductionReminderMember.objects.acreate(member_id=member.id)

    class OptOutIntroductionRemindersView(View):
        """
        A discord.View containing a button to opt-in/out of introduction reminders.

        This discord.View contains a single button that can change the state of whether the
        member will be sent reminders to send an introduction message in the
        CSS Discord server.
        The view object will be sent to the member's DMs, after a delay period after
        joining the CSS Discord server.
        """

        def __init__(self, bot: TeXBot) -> None:
            """Initialize a new discord.View, to opt-in/out of introduction reminders."""
            self.bot: TeXBot = bot

            super().__init__(timeout=None)

        @ui.button(
            label="Opt-out of introduction reminders",
            custom_id="opt_out_introduction_reminders_button",
            style=discord.ButtonStyle.red,
            emoji=discord.PartialEmoji.from_str(
                emoji.emojize(":no_good:", language="alias")
            )
        )
        async def opt_out_introduction_reminders_button_callback(self, button: discord.Button, interaction: discord.Interaction) -> None:  # noqa: E501
            """
            Set the opt-in/out flag depending on the status of the button.

            This function is attached as a button's callback, so will run whenever the button
            is pressed.
            """
            try:
                guild: discord.Guild = self.bot.css_guild
            except GuildDoesNotExist as guild_error:
                logging.critical(guild_error)
                await self.bot.close()
                return

            if not interaction.user:
                await interaction.response.send_message(
                    (
                        ":warning:There was an error when trying to opt-in/out of"
                        " introduction reminders.:warning:"
                    ),
                    ephemeral=True
                )
                return

            interaction_member: discord.Member | None = guild.get_member(interaction.user.id)

            button_will_make_opt_out: bool = (
                    button.style == discord.ButtonStyle.red
                    or str(button.emoji) == emoji.emojize(":no_good:", language="alias")
                    or bool(button.label and "Opt-out" in button.label)
            )
            button_will_make_opt_in: bool = (
                    button.style == discord.ButtonStyle.green
                    or str(button.emoji) == emoji.emojize(
                        ":raised_hand:",
                        language="alias"
                    )
                    or bool(button.label and "Opt back in" in button.label))

            if button_will_make_opt_out:
                if not interaction_member:
                    await interaction.response.send_message(
                        (
                            ":warning:There was an error when trying to opt-out of"
                            " introduction reminders.:warning:\n`You must be a member of"
                            " the CSS Discord server to opt-out of introduction reminders.`"
                        ),
                        ephemeral=True
                    )
                    return

                try:
                    await IntroductionReminderOptOutMember.objects.acreate(
                        member_id=interaction_member.id
                    )
                except ValidationError as create_introduction_reminder_opt_out_member_error:
                    error_is_already_exists: bool = (
                        "hashed_member_id" in create_introduction_reminder_opt_out_member_error.message_dict  # noqa: E501
                        and any(
                            "already exists" in error
                            for error
                            in create_introduction_reminder_opt_out_member_error.message_dict[
                                "hashed_member_id"
                            ]
                        )
                    )
                    if not error_is_already_exists:
                        raise

                button.style = discord.ButtonStyle.green
                button.label = "Opt back in to introduction reminders"
                button.emoji = discord.PartialEmoji.from_str(
                    emoji.emojize(":raised_hand:", language="alias")
                )

                await interaction.response.edit_message(view=self)

            elif button_will_make_opt_in:
                if not interaction_member:
                    await interaction.response.send_message(
                        (
                            ":warning:There was an error when trying to opt back in"
                            " to introduction reminders.:warning:\n`You must be a member of"
                            " the CSS Discord server to opt back in to"
                            " introduction reminders.`"
                        ),
                        ephemeral=True
                    )
                    return

                try:
                    introduction_reminder_opt_out_member: IntroductionReminderOptOutMember = await IntroductionReminderOptOutMember.objects.aget(  # noqa: E501
                        hashed_member_id=IntroductionReminderOptOutMember.hash_member_id(
                            interaction_member.id
                        )
                    )
                except IntroductionReminderOptOutMember.DoesNotExist:
                    pass
                else:
                    await introduction_reminder_opt_out_member.adelete()

                button.style = discord.ButtonStyle.red
                button.label = "Opt-out of introduction reminders"
                button.emoji = discord.PartialEmoji.from_str(
                    emoji.emojize(":no_good:", language="alias")
                )

                await interaction.response.edit_message(view=self)

    @send_introduction_reminders.before_loop
    async def before_tasks(self) -> None:
        """Pre-execution hook, preventing any tasks from executing before the bot is ready."""
        await self.bot.wait_until_ready()

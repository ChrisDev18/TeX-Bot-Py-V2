"""Contains cog classes for any make_member interactions."""

import logging
import re
from typing import Final

import aiohttp
import bs4
import discord
from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError

from cogs._utils import TeXBotCog
from config import settings
from db.core.models import UoBMadeMember
from exceptions import GuildDoesNotExist, MemberRoleDoesNotExist


class MakeMemberCommandCog(TeXBotCog):
    # noinspection SpellCheckingInspection
    """Cog class that defines the "/makemember" command and its call-back method."""

    # noinspection SpellCheckingInspection
    @discord.slash_command(  # type: ignore[no-untyped-call, misc]
        name="makemember",
        description="Gives you the Member role when supplied with an appropriate Student ID."
    )
    @discord.option(  # type: ignore[no-untyped-call, misc]
        name="studentid",
        description="Your UoB Student ID",
        input_type=str,
        required=True,
        max_length=7,
        min_length=7,
        parameter_name="uob_id"
    )
    async def make_member(self, ctx: discord.ApplicationContext, uob_id: str) -> None:
        """
        Definition & callback response of the "make_member" command.

        The "make_member" command validates that the given member has a valid CSS membership
        then gives the member the "Member" role.
        """
        try:
            guild: discord.Guild = self.bot.css_guild
        except GuildDoesNotExist as guild_error:
            await self.send_error(ctx, error_code="E1011")
            logging.critical(guild_error)
            await self.bot.close()
            return

        member_role: discord.Role | None = await self.bot.member_role
        if not member_role:
            await self.send_error(
                ctx,
                error_code="E1023",
                logging_message=str(MemberRoleDoesNotExist())
            )
            return

        interaction_member: discord.Member | None = guild.get_member(ctx.user.id)
        if not interaction_member:
            await self.send_error(
                ctx,
                message="You must be a member of the CSS Discord server to use this command."
            )
            return

        if member_role in interaction_member.roles:
            await ctx.respond(
                (
                    ":information_source: No changes made. You're already a member"
                    " - why are you trying this again? :information_source:"
                ),
                ephemeral=True
            )
            return

        if not re.match(r"\A\d{7}\Z", uob_id):
            await self.send_error(
                ctx,
                message=f"\"{uob_id}\" is not a valid UoB Student ID."
            )
            return

        uob_id_already_used: bool = await UoBMadeMember.objects.filter(
            hashed_uob_id=UoBMadeMember.hash_uob_id(uob_id)
        ).aexists()
        if uob_id_already_used:
            committee_mention: str = "committee"

            committee_role: discord.Role | None = await self.bot.committee_role
            if committee_role:
                committee_mention = committee_role.mention

            await ctx.respond(
                (
                    ":information_source: No changes made. This student ID has already"
                    f" been used. Please contact a {committee_mention} member if this is"
                    " an error. :information_source:"
                ),
                ephemeral=True
            )
            return

        guild_member_ids: set[str] = set()

        request_headers: dict[str, str] = {
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Expires": "0"
        }
        request_cookies: dict[str, str] = {".ASPXAUTH": settings["MEMBERS_PAGE_COOKIE"]}
        async with aiohttp.ClientSession(headers=request_headers, cookies=request_cookies) as http_session:  # noqa: E501, SIM117
            async with http_session.get(url=settings["MEMBERS_PAGE_URL"]) as http_response:
                response_html: str = await http_response.text()

        MEMBER_HTML_TABLE_IDS: Final[frozenset[str]] = frozenset(
            {
                "ctl00_Main_rptGroups_ctl05_gvMemberships",
                "ctl00_Main_rptGroups_ctl03_gvMemberships"
            }
        )
        table_id: str
        for table_id in MEMBER_HTML_TABLE_IDS:
            parsed_html: bs4.Tag | None = BeautifulSoup(
                response_html,
                "html.parser"
            ).find(
                "table",
                {"id": table_id}
            )

            if parsed_html:
                guild_member_ids.update(
                    row.contents[2].text
                    for row
                    in parsed_html.find_all(
                        "tr",
                        {"class": ["msl_row", "msl_altrow"]}
                    )
                )

        guild_member_ids.discard("")
        guild_member_ids.discard("\n")
        guild_member_ids.discard(" ")

        if not guild_member_ids:
            await self.send_error(
                ctx,
                error_code="E1041",
                logging_message=OSError(
                    "The guild member IDs could not be retrieved from"
                    " the MEMBERS_PAGE_URL."
                )
            )
            return

        if uob_id not in guild_member_ids:
            await self.send_error(
                ctx,
                message=(
                    "You must be a member of The Computer Science Society to use this command."
                    "\nThe provided student ID must match the UoB student ID"
                    " that you purchased your CSS membership with."
                )
            )
            return

        await ctx.respond("Successfully made you a member!", ephemeral=True)

        # NOTE: The "Member" role must be added to the user **before** the "Guest" role to ensure that the welcome message does not include the suggestion to purchase membership
        await interaction_member.add_roles(
            member_role,
            reason="TeX Bot slash-command: \"/makemember\""
        )

        guest_role: discord.Role | None = await self.bot.guest_role
        if not guest_role:
            logging.warning(
                "\"/makemember\" command used but the \"Guest\" role does not exist."
                " Some user's may now have the \"Member\" role without the \"Guest\" role."
                " Use the \"/ensure-members-inducted\" command to fix this issue."
            )
        elif guest_role not in interaction_member.roles:
            await interaction_member.add_roles(
                guest_role,
                reason="TeX Bot slash-command: \"/makemember\""
            )

        applicant_role: discord.Role | None = discord.utils.get(
            self.bot.css_guild.roles,
            name="Applicant"
        )
        if applicant_role and applicant_role in interaction_member.roles:
            await interaction_member.remove_roles(
                applicant_role,
                reason="TeX Bot slash-command: \"/makemember\""
            )

        try:
            await UoBMadeMember.objects.acreate(uob_id=uob_id)
        except ValidationError as create_uob_made_member_error:
            error_is_already_exists: bool = (
                "hashed_uob_id" in create_uob_made_member_error.message_dict
                and any(
                    "already exists"
                    in error
                    for error
                    in create_uob_made_member_error.message_dict["hashed_uob_id"]
                )
            )
            if not error_is_already_exists:
                raise

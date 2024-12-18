import os
from typing import Literal

import binascii
import jwt
from dotenv import load_dotenv
from jwt.exceptions import DecodeError, ExpiredSignatureError

from algos.shift import ShiftCipher, BShiftCipher
from cogs.CSR import CSR
from cogs.ElGamalAuthentication import ElGamalAuthentication
from commands.eph_dh import get_ec_keys, fetch_session_key, aes_decrypt
from utils.constants import Client, init_keys, Secrets, Keys

load_dotenv()
GUILD_IDS = [int(os.getenv("GUILD_ID"))]
ROLE_ID = int(os.getenv("ROLE_ID"))

USER_DATA_DIR = "userdata"
USED_TOKENS_FILE = "used_tokens.txt"

if not os.path.isfile(USED_TOKENS_FILE):
    open(USED_TOKENS_FILE, "w").close()

if not os.path.isdir(USER_DATA_DIR):
    os.mkdir(USER_DATA_DIR)

import nextcord
from nextcord.ext import commands, application_checks
from nextcord.ext.application_checks import ApplicationMissingPermissions
from nextcord import SlashOption

bot = commands.Bot()


async def register_user(interaction: nextcord.Interaction, token: str) -> tuple[bool, str]:
    # User is already registered, maybe with another token.
    # By design, if the user has left the server and rejoins, no automatic
    # or token re-registration is possible!
    user_id = interaction.user.id
    user_datafile = f"{USER_DATA_DIR}/{user_id}.txt"

    if os.path.isfile(user_datafile):
        return False, "You are already registered"

    try:
        data = jwt.decode(token, Secrets.JWT_SECRET, algorithms="HS256")
    except (DecodeError, ExpiredSignatureError) as err:
        print(f"User '{user_id}' submitted invalid token '{token}'")
        return False, str(err)

    with open(USED_TOKENS_FILE, "r") as f:
        used_tokens = f.readlines()

    token_string = f"{token}\n"
    if token_string in used_tokens:
        return False, "Token already used"

    try:
        await interaction.user.add_roles(interaction.guild.get_role(ROLE_ID))
    except nextcord.errors.Forbidden:
        return False, "Bot lacks necessary permissions"

    # Do not expire the token or register the user before
    # the user actually has the role.
    with open(user_datafile, "w") as f:
        f.write(f"{data['name']}\n{data['studentCode']}")

    with open(USED_TOKENS_FILE, "a") as f:
        f.write(token_string)

    return True, ""


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.slash_command(description="Register as a student.", guild_ids=GUILD_IDS)
async def register(interaction: nextcord.Interaction, token: str):
    ok, err = await register_user(interaction, token)
    if ok:
        message = "Registered successfully"
    else:
        message = f"Registration error! {err}"
    await interaction.send(message, ephemeral=True)


@bot.slash_command(description="Who am I?", guild_ids=GUILD_IDS)
async def whoami(interaction: nextcord.Interaction):
    user_id = interaction.user.id
    user_datafile = f"{USER_DATA_DIR}/{user_id}.txt"

    if not os.path.isfile(user_datafile):
        await interaction.send("I don't know :(", ephemeral=True)
        return

    with open(user_datafile, "r") as f:
        user_data = f.readlines()

    await interaction.send(user_data[1], ephemeral=True)


@bot.slash_command(description="Identify the member.", guild_ids=GUILD_IDS)
@application_checks.has_guild_permissions(administrator=True)  # Server integration failsafe
async def whois(interaction: nextcord.Interaction, user: nextcord.Member):
    user_datafile = f"{USER_DATA_DIR}/{user.id}.txt"

    if not os.path.isfile(user_datafile):
        await interaction.send("unknown", ephemeral=True)
        return

    with open(user_datafile, "r") as f:
        user_data = f.readlines()

    await interaction.send(user_data[1], ephemeral=True)


@whois.error
async def whois_error(interaction: nextcord.Interaction, error):
    if isinstance(error, ApplicationMissingPermissions):
        await interaction.send("Unauthorised", ephemeral=True)


MSG_LEN_MAX = 100


@bot.slash_command(description="Use the shift cipher.", dm_permission=True)
async def shift(interaction: nextcord.Interaction,
                action: Literal["enc", "dec"] = SlashOption(
                    description="The operation to perform.",
                    choices=["enc", "dec"]),
                key: int = SlashOption(
                    description="The shift key.",
                    min_value=0, max_value=26),
                data: str = SlashOption(
                    description=f"The plaintext or the ciphertext (max {MSG_LEN_MAX} characters).",
                    max_length=MSG_LEN_MAX)):
    cipher = ShiftCipher(key)
    try:
        match action:
            case "enc":
                res = cipher.encrypt(data)
            case "dec":
                res = cipher.decrypt(data)
            case _:
                await interaction.send(f"Unknown action '{action}'", ephemeral=True)
                return

        await interaction.send(res, ephemeral=True)
    except RuntimeError as e:
        await interaction.send(f"The {str(e)}!", ephemeral=True)


@bot.slash_command(description="Use the binary shift cipher.", dm_permission=True)
async def bshift(interaction: nextcord.Interaction,
                 action: Literal["enc", "dec"] = SlashOption(
                     description="The operation to perform.",
                     choices=["enc", "dec"]),
                 key: int = SlashOption(
                     description="The shift key.",
                     min_value=0, max_value=255),
                 data: str = SlashOption(
                     description=f"The plaintext or the ciphertext (max {MSG_LEN_MAX} characters). "
                                 "Ciphertexts must be base64-encoded!",
                     max_length=MSG_LEN_MAX)):
    cipher = BShiftCipher(key)
    match action:
        case "enc":
            res = cipher.encrypt_strings(data)
        case "dec":
            try:
                res = cipher.decrypt_strings(data)
            except binascii.Error:
                await interaction.send("Ciphertext is not a valid base64 string!", ephemeral=True)
                return
        case _:
            await interaction.send(f"Unknown action '{action}'", ephemeral=True)
            return

    await interaction.send(res, ephemeral=True)


@bot.slash_command(description="List public keys.", dm_permission=True)
async def lpk(interaction: nextcord.Interaction):
    pub = Keys.P384.public_key()
    # Use singe quotes here since the backticks confuse some interpreters.
    pub_pem = f'```{pub.export_key(format="PEM")}```'
    await interaction.send(pub_pem, ephemeral=True)
    await interaction.send(Keys.EG.pk, ephemeral=True)


@bot.slash_command(description="Share AES-128 key with DH and decrypt.", dm_permission=True)
async def dh_aes(interaction: nextcord.Interaction,
                 s_key: str = SlashOption(description="Your (long term) public key."),
                 e_key: str = SlashOption(description="Your ephemeral public key."),
                 ct: str = SlashOption(description="The AES-128 encrypted message (hex).", max_length=128),
                 iv: str = SlashOption(description="The AES-128 initialization vector (hex).",
                                       min_length=32, max_length=32)):
    try:
        s_pk, e_pk = await get_ec_keys(interaction, s_key, e_key)
    except RuntimeError:
        return

    session_key = fetch_session_key(Keys.P384, s_pk, e_pk)

    try:
        message = await aes_decrypt(interaction, ct, iv, session_key)
    except RuntimeError:
        return

    await interaction.send(message, ephemeral=True)


init_keys()

from utils import database

database.connect()

bot.add_cog(ElGamalAuthentication(bot))
bot.add_cog(CSR(bot))

bot.run(Client.TOKEN)

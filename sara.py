import asyncio
import discord
from discord.ext import commands

import json

import dateutil, datetime
from dateutil.parser import parse as timeparse
from dateutil.parser._parser import ParserError

CONFIG_FILE = 'config.json'
with open(CONFIG_FILE, 'r') as f:
    config = json.load(f)
with open(config['auth_file'], 'r') as f:
    config.update(json.load(f))
bot = commands.Bot(commands.when_mentioned_or(config['command_prefix']))

import logging
logging.basicConfig(level=logging.INFO)


# generic helpers

def list_group(l, key=lambda x: x):
    groups = {}
    for elem in l:
        if key(elem) not in groups:
            groups[key(elem)] = []
        groups[key(elem)].append(elem)
    return list(zip(groups.keys(), groups.values()))

def list_split(l, delim):
    parts = [[]]
    for item in l:
        if item == delim:
            parts.append([])
        else:
            parts[-1].append(item)
    return parts

def bot_command_args(args):
    return list(map(lambda arg: ' '.join(arg), list_split(args, config['command_prefix'])))


def try_get_X_named(_type, xs, name):
    matches = list(filter(lambda x: x.name == name, xs))
    if not matches:
        raise ValueError(config['error_messages']['X_not_found'].format(_type, channel_name))
    if len(matches) > 1:
        raise UserWarning(config['error_messages']['multiple_X_found'].format(_type, channel_name))
    return matches[0]

def try_get_channel_named(guild, channel_name):
    return try_get_X_named('channel', guild.channels, channel_name)

def try_get_role_named(guild, role_name):
    return try_get_X_named('role', guild.roles, role_name)


# scheduling messages

async def store_batch_scheduled_messages(batchfile, guild_id, channel_id):
    with open(config['scheduled_message_store'], 'r') as f:
        stored = json.load(f)
    with open(batchfile, 'r') as f:
        batched_spec = json.load(f)

    batched_messages = [{'iso_time': iso_time, 'msg': msg['message'],
                         'guild_id': guild_id, 'channel_id': channel_id, 'batch': True}
        for msg in batched_spec for iso_time in msg['times']]
    stored['messages'].extend(batched_messages)

    with open(config['scheduled_message_store'], 'w') as f:
        json.dump(stored, f)

    return list_group(stored_strings_to_objects(batched_messages),
                      key=lambda m: m['time'])

async def send_batch_scheduled_messages(time, messages):
    now_time = datetime.datetime.now()
    if time > now_time:
        await asyncio.sleep((time - now_time).total_seconds())
        for message in messages:
            await message['channel'].send(message['msg'])

async def send_batches_scheduled_messages(batches):
    await asyncio.gather(*(send_batch_scheduled_messages(time, messages)
        for time, messages in batches))


async def store_scheduled_message(iso_time, msg, guild_id, channel_id):
    with open(config['scheduled_message_store'], 'r') as f:
        stored = json.load(f)
    stored['messages'].append({'iso_time': iso_time, 'msg': msg,
                               'guild_id': guild_id, 'channel_id': channel_id})
    with open(config['scheduled_message_store'], 'w') as f:
        json.dump(stored, f)

async def send_scheduled_message(message):
    now_time = datetime.datetime.now()
    if message['time'] > now_time:
        await asyncio.sleep((message['time'] - now_time).total_seconds())
        await message['channel'].send(message['msg'])


@bot.listen('on_ready')
async def load_stored_scheduled_messages():
    try:
        with open(config['scheduled_message_store'], 'r') as f:
            stored = json.load(f)
    except FileNotFoundError:
        with open(config['scheduled_message_store'], 'w') as f:
            stored = {'messages': []}
            json.dump(stored, f)

    batched = []
    unbatched = []
    for message in stored_strings_to_objects(stored['messages']):
        if 'batch' in message and message['batch']:
            batched.append(message)
        else:
            unbatched.append(message)
    batched = list_group(stored_strings_to_objects(batched), key=lambda m: m['time'])

    await asyncio.gather(
        asyncio.gather(*(send_scheduled_message(message)
            for message in unbatched)),
        asyncio.gather(*(send_batches_scheduled_messages(time, messages)
            for time, messages in batched)))


def stored_strings_to_objects(stored):
    future_messages = []
    for message in stored:
        parsed = timeparse(message['iso_time'])
        channel = bot.get_guild(message['guild_id']).get_channel(message['channel_id'])
        future_messages.append({'time': parsed, 'msg': message['msg'], 'channel': channel})
    return future_messages


class Security(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.moderation_queue = {}

    async def schedule_message(self, ctx, *args):
        args = bot_command_args(args)
        msg = args[1]

        if args[0].lower() == 'now':
            time = datetime.datetime.now() + datetime.timedelta(seconds=3)
        else:
            time = timeparse(args[0])
        if time < datetime.datetime.now():
            await ctx.send(config['error_messages']['schedule_past'])
            return False

        try:
            channel = try_get_channel_named(ctx.guild, args[2].lstrip('#'))
        except ValueError as e:
            await ctx.send(str(e))
            return False
        except UserWarning as e:
            await ctx.send(str(e))

        await store_scheduled_message(str(time), msg, ctx.guild.id, channel.id)
        await ctx.message.add_reaction('👍')
        await send_scheduled_message({'time': time, 'msg': msg, 'channel': channel})
        return True

    @commands.command(name='schedule-full')
    @commands.has_role(config['roles']['security'])
    async def schedule_command_full(self, ctx, *args):
        await self.schedule_message(ctx, *args)

    @commands.command(name='schedule')
    @commands.has_role(config['roles']['security'])
    async def schedule_command(self, ctx, *args):
        await self.schedule_message(ctx, *args, '$', config['default_scheduled_send_channel'])

    @commands.command(name='schedule-batch')
    @commands.has_role(config['roles']['security'])
    async def schedule_batch(self, ctx, batchfile):
        send_channel = try_get_channel_named(ctx.guild, config['default_scheduled_send_channel'])
        batches = await store_batch_scheduled_messages(batchfile, ctx.guild.id, send_channel.id)
        await ctx.message.add_reaction('👍')
        await send_batches_scheduled_messages(batches)

    @commands.command(name='request')
    @commands.guild_only()
    async def request(self, ctx, *args):
        security_role = try_get_role_named(ctx.guild, config['roles']['security'])
        approval_channel = try_get_channel_named(ctx.guild, config['approve_channel'])
        queued = await approval_channel.send('{mention} {sender} in {channel} says:\n"{message}"'.format(
            mention=security_role.mention,
            sender=ctx.author.nick or ctx.author.name,
            channel=ctx.channel.mention,
            message=ctx.message.content))
        self.moderation_queue[queued.id] = ctx.message
        await ctx.message.add_reaction('🦺')

    REQUEST_LEN = len(request.name) + len(config['command_prefix'])

    @commands.command(name='approve')
    @commands.has_role(config['roles']['security'])
    async def approve(self, ctx, *args):
        send_channel = try_get_channel_named(ctx.guild, config['default_scheduled_send_channel'])
        if ctx.message.reference and ctx.message.reference.message_id in self.moderation_queue:
            orig_msg = self.moderation_queue[ctx.message.reference.message_id]
            reference = '({} in {})'.format(
                orig_msg.author.nick or orig_msg.author.name,
                orig_msg.channel.mention)
            if len(args) == 0:
                await send_channel.send('{} {}'.format(orig_msg.content[REQUEST_LEN:], reference))
                flag = True
            elif args[-1] == 'unchanged':
                flag = await self.schedule_command(ctx, *args[:-1], orig_msg.content[REQUEST_LEN:], reference)
            else:
                flag = await self.schedule_command(ctx, *args, reference)
            if flag:
                del self.moderation_queue[ctx.message.reference.message_id]
        else:
            await ctx.send(config['error_messages']['reply_to_approve'])

    @commands.command(name='show-until')
    @commands.has_role(config['roles']['security'])
    async def show_until(self, ctx, until):
        until_time = timeparse(until)
        now_time = datetime.datetime.now()
        with open(config['scheduled_message_store'], 'r') as f:
            stored = stored_strings_to_objects(json.load(f)['messages'])
        to_show = sorted(list_group(filter(lambda x: now_time < x['time'] <= until_time,
                                           stored),
                                    key=lambda x: x['time']),
                         key=lambda x: x[0])
        for time, messages in to_show:
            text = ['at {}\n'.format(time)]
            for message in messages:
                text.append('{0[channel].mention}: {0[msg]}\n'.format(message))
            await ctx.send(''.join(text), allowed_mentions=discord.AllowedMentions.none())


    #@commands.Cog.listener('on_command_error')
    async def catch_role_errors(self, ctx, error):
        if type(error) is commands.MissingRole:
            await ctx.send(config['error_messages']['need_role'].format(error.missing_role))
        elif type(error) is commands.NoPrivateMessage:
            await ctx.send(config['error_messages']['no_DM'])
        else:
            await ctx.send('{}: {}'.format(type(error).__name__, error))


class Admin(commands.Cog):

    @commands.command(name='reload-config')
    @commands.has_role(config['roles']['admin'])
    async def reload_config(self, ctx):
        global config
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                await ctx.message.add_reaction('👍')
        except (JSONDecodeError, OSError) as e:
            await ctx.send(config['error_messages']['config_reload_error'].format(str(e)))


from random import choice
class General(commands.Cog):

    greetings = ['hello!', 'hi there', 'yo.', 'howdy', 'heyyy :))', 'hiii ;)',
        'salut mon ami(e)', '👋', '🤘', '🤙', 'go away']

    @commands.command(name='hello')
    async def hello(self, ctx):
        await ctx.send(choice(self.greetings))


bot.add_cog(Security(bot))
bot.add_cog(Admin(bot))
bot.add_cog(General(bot))
bot.run(config['discord_auth_token'])

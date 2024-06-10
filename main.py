"""
vidifierbot

Copyright (c) 2021-2022 classabbyamp, 2024 cschmittiey
Released under the BSD-3-Clause license
"""

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Optional
import traceback
import json
import html
import re
import os
from copy import deepcopy
from signal import SIGINT, SIGTERM, SIGABRT, SIGUSR1, SIGUSR2

import yt_dlp
import telegram as tg
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, ContextTypes

from data import keys

# Enable logging
logging.basicConfig(format='[%(asctime)s] [%(levelname)s] %(name)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

HELP_TEXT = ""
with Path("./data/help.md").open() as hf:
    HELP_TEXT = hf.read()

YDL_OPTS = {
    "outtmpl": f"{keys.tempdir}/%(id)s.%(ext)s",
    "logger": logger,
    "max_filesize": 49_500_000,
    "format": "best[ext=mp4]/bestvideo+bestaudio",
    "merge_output_format": "mp4",
    "noplaylist": True,
    "postprocessors": [],
}

YDL_OPTS_GIF = deepcopy(YDL_OPTS)
YDL_OPTS_GIF["outtmpl"] = f"{keys.tempdir}/%(id)s_gif.%(ext)s"
YDL_OPTS_GIF["postprocessors"].append({
    "key": "Exec",
    "exec_cmd": "ffmpeg -y -v 16 -i {} -c copy -an {}.mp4 && mv {}.mp4 {}",
})

class InternalError(Exception):
    def __init__(self, msg: str):
        self.msg = msg

async def help_command(update: Update, context: CallbackContext):
    if update.message:
        await update.message.reply_text(HELP_TEXT, disable_web_page_preview=True, parse_mode=tg.constants.ParseMode.MARKDOWN_V2)

async def shutdown_command(update: Update, context: CallbackContext):
    if update.effective_user:
        if update.effective_user.id == keys.owner_id:
            logger.info(f"Shutdown command received from {update.effective_user.id}")
            await context.bot.send_message(chat_id=keys.owner_id, text="Shutting down", parse_mode=tg.constants.ParseMode.MARKDOWN_V2)
            os.kill(os.getpid(), SIGUSR1)
            return
        logger.info(f"Shutdown command received from {update.effective_user.id}, ignoring")
        return
    logger.info("Shutdown command received from unknown user, ignoring")

async def restart_command(update: Update, context: CallbackContext):
    if update.effective_user:
        if update.effective_user.id == keys.owner_id:
            logger.info(f"Restart command received from {update.effective_user.id}")
            await context.bot.send_message(chat_id=keys.owner_id, text="Restarting", parse_mode=tg.constants.ParseMode.MARKDOWN_V2)
            os.kill(os.getpid(), SIGUSR2)
            return
        logger.info(f"Restart command received from {update.effective_user.id}, ignoring")
        return
    logger.info("Restart command received from unknown user, ignoring")

async def vidify_command(update: Update, context: CallbackContext):
    await run_cmd(update, gif=False)

async def gifify_command(update: Update, context: CallbackContext):
    await run_cmd(update, gif=True)

async def run_cmd(update: Update, gif: bool):
    urls = []
    if update.message:
        if update.message.text:
            urls += list(update.message.parse_entities(types=["url"]).values())

        if update.message.reply_to_message:
            urls += list(update.message.reply_to_message.parse_entities(types=["url"]).values())

        if urls:
            await get_and_send_videos(update.message, urls, gif)
        else:
            await update.message.reply_text(("Unable to find any URLs to convert in your message or the replied-to message. "
                                             "If this is a private group, I probably can't see the replied-to message."))

async def get_and_send_videos(msg: tg.Message, urls: list[str], gif: bool = False):
    logger.info(f"[{msg.message_id}] {', '.join(urls)}")
    opts: dict = YDL_OPTS if not gif else YDL_OPTS_GIF
    try:
        if trim := parse_timestamp(msg):
            opts["postprocessors"].append(trim)
    except InternalError as e:
        logger.error(f"[{msg.message_id}] {e.msg}")
        await msg.reply_text(f"{e.msg}\nid: {msg.message_id}", quote=True, disable_web_page_preview=True)
        return
    with yt_dlp.YoutubeDL(opts) as ydl:
        for url in urls:
            ydl.cache.remove()
            fn = ""
            try:
                info = ydl.extract_info(url, download=True)
                v_id = info["id"]
                fn = ydl.prepare_filename(info)
                if trim:
                    fn += ".trim.mp4"
            except Exception as e:
                logger.error(f"[{msg.message_id}] {type(e)}: {e}")
                await msg.reply_text(f"Unable to find video at {url}\nid: {msg.message_id}",
                                     quote=True, disable_web_page_preview=True)
            else:
                await send_videos(msg, url, fn, v_id, gif)
                f = Path(fn)
                if f.is_file():
                    f.unlink()

async def send_videos(msg: tg.Message, url: str, fn: str, v_id: str, gif: bool = False):
    if fn:
        fp = Path(fn)
        # check vid is <50MB
        if fp.is_file() and fp.stat().st_size < 49_500_000:
            with fp.open("rb") as video:
                try:
                    if gif:
                        await msg.reply_animation(animation=video, caption=url, quote=True)
                    else:
                        await msg.reply_video(video=video, caption=url, quote=True, write_timeout=60)
                except tg.error.TelegramError as e:
                    logger.error(f"[{v_id}] {e}")
                    await msg.reply_text(f"Unable to upload video from {url}\nid: {v_id}",
                                         quote=True, disable_web_page_preview=True)
        else:
            logger.error(f"[{v_id}] file does not exist or too large for upload")
            await msg.reply_text(f"Unable to find video at {url}, or video is too large to upload\nid: {v_id}",
                                 quote=True, disable_web_page_preview=True)
    else:
        logger.error(f"[{v_id}] file does not exist")
        await msg.reply_text(f"Unable to find video at {url}\nid: {v_id}",
                             quote=True, disable_web_page_preview=True)

def parse_timestamp(msg: tg.Message) -> Optional[dict[str, str]]:
    start = None
    end = None
    dur = None

    if msg.text:
        text = filter_text_entities(msg).lower()
        start = get_timestamp(["s", "start"], text)
        end = get_timestamp(["e", "end"], text)
        dur = get_timestamp(["d", "dur", "duration"], text)

        if end is not None or dur is not None:
            if start is None:
                start = "0"
            flag = "-to" if end is not None else "-t"
            endur = end if end is not None else dur
            cmd = " ".join(["ffmpeg -y -v 16 -ss", start, flag, endur, "-i {} -c copy -acodec copy {}.trim.mp4"])
            return {
                "key": "ExecAfterDownload",
                "exec_cmd": cmd,
            }
        elif start is not None:
            raise InternalError("Must provide end= or dur= with start=")
    return None

def filter_text_entities(msg: tg.Message) -> str:
    entities = msg.parse_entities(types=["url", "email", "mention", "hashtag", "bot_command", "text_link",
                                         "text_mention", "phone_number", "cashtag"])
    text = msg.text
    if text is not None:
        for en in entities.values():
            text = text.replace(en, "")
        return text
    return ""

def get_timestamp(pfxs: list[str], text: str) -> Optional[str]:
    TS = (r"(?:(?P<sfrac>[0-9]+(?:\.[0-9]+)?(?:s|ms|us))|"
          r"(?:(?:(?P<h>[0-9]+):)?(?P<m>[0-9]{1,2}):)?(?P<s>[0-9]{1,2}(?:\.[0-9]+)?))")
    ts_val = None

    if matches := re.search(fr"(?:{'|'.join(pfxs)})=" + TS, text):
        g = matches.groupdict()
        if "sfrac" in g and g["sfrac"] is not None:
            ts_val = g["sfrac"]
        elif "s" in g and g["s"] is not None:
            h = g["h"] if g["h"] is not None else "0"
            m = g["m"] if g["m"] is not None else "0"
            s = g["s"] if g["s"] is not None else "0"
            ts_val = f"{int(h):0>2}:{int(m):0>2}:{float(s):02.3f}"
    return ts_val

async def cleanup_files(context: ContextTypes.DEFAULT_TYPE):
    # delete mp4 files in keys.tempdir older than 5 min
    deleted = 0
    del_age = timedelta(minutes=5)
    tmp = Path(keys.tempdir)
    files = tmp.glob("*.mp4")
    for f in files:
        file_creation_time = datetime.fromtimestamp(f.stat().st_ctime, timezone.utc)
        age = abs(datetime.now(timezone.utc) - file_creation_time)
        if f.is_file() and age > del_age:
            f.unlink()
            deleted += 1
    if deleted:
        logger.info(f"Deleted {deleted} cached mp4 files from {keys.tempdir}")

async def error_handler(update: object, context: CallbackContext):
    logger.error("Exception while handling an update:", exc_info=context.error)

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)  # type: ignore
    tb_string = ''.join(tb_list)

    update_str = await update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        f'An exception was raised while handling an update\n'
        f'<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}'
        '</pre>\n\n'
        f'<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n'
        f'<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n'
        f'<pre>{html.escape(tb_string)}</pre>'
    )

    await context.bot.send_message(chat_id=keys.owner_id, text=message, parse_mode=tg.constants.ParseMode.HTML)

if __name__ == "__main__":
    application = Application.builder().token(keys.tg_token).build()

    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("shutdown", shutdown_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("vidify", vidify_command))
    application.add_handler(CommandHandler("gifify", gifify_command))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Entity("url"), vidify_command))
    application.add_error_handler(error_handler)

    job_queue = application.job_queue
    job_queue.run_repeating(cleanup_files, interval=timedelta(minutes=5), first=10)

    application.run_polling(allowed_updates=Update.ALL_TYPES)

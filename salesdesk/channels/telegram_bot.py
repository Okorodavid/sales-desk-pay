"""Telegram: customers can message the bot (handy for demos on any phone), and the owner's group gets
hand-overs, payment notes and the morning brief. Replying to a hand-over in that group sends the reply
to the customer and takes over the chat."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

log = logging.getLogger("salesdesk.telegram")


def build(token: str, owner_chat_id: int | None, desk, dispatcher) -> Application:
    from ..views import brief_text

    app = Application.builder().token(token).build()

    async def on_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text:
            return
        text = "Hi" if msg.text.startswith("/start") else msg.text
        await context.bot.send_chat_action(msg.chat_id, "typing")
        name = update.effective_user.first_name if update.effective_user else ""
        await dispatcher.deliver(await desk.handle("telegram", str(msg.chat_id), text, name))

    async def on_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.text or not msg.reply_to_message:
            return
        row = desk.store.handoff_by_ref(f"tg:{msg.reply_to_message.message_id}")
        if not row:
            return
        outcome = desk.owner_reply(row["customer_id"], msg.text)
        await dispatcher.deliver(outcome)
        await msg.reply_text("Sent. You've taken over this chat; give it back to the robot on the board." if outcome.replies
                             else "Couldn't send (they may have opted out).")

    async def brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.id == owner_chat_id:
            await update.effective_message.reply_text(brief_text(desk))

    app.add_handler(CommandHandler("brief", brief))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_private))
    if owner_chat_id:
        app.add_handler(MessageHandler(filters.Chat(owner_chat_id) & filters.TEXT & ~filters.COMMAND, on_owner))
    return app

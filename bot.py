elif data == "cancel_download":
    task = active_downloads.get(user.id)
    if task and not task.done():
        task.cancel()
    else:
        await query.edit_message_text(
            "دانلود فعالی برای لغو وجود ندارد.",
            reply_markup=main_menu_keyboard(),
        )

elif data == "retry_download":
    url = context.user_data.get("last_url")
    chat_id = context.user_data.get("last_chat_id")
    msg_id = context.user_data.get("last_message_id")
    if not url or not chat_id or not msg_id:
        await query.edit_message_text(
            "لینک قبلی پیدا نشد.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # پیام جدید برای دانلود مجدد
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔁 تلاش دوباره برای دانلود:\n{url}",
            reply_markup=download_control_keyboard(),
        )
        fake_update = Update(
            update.update_id,
            message=msg,
        )
        await process_download_request(fake_update, context, url)
    except Exception as e:
        logger.error(f"خطا در تلاش دوباره: {e}")
        await query.edit_message_text(
            "خطا در تلاش دوباره.",
            reply_markup=main_menu_keyboard(),
        )
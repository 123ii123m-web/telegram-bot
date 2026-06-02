from telegram.ext import Application, CommandHandler

async def start(update, context):
    await update.message.reply_text("ربات روشنه امیر جان!")

app = Application.builder().token("8603497838:AAGm1LIeZJ9B-hBh3ry7YOy8uAWjZEmC1W8").build()

app.add_handler(CommandHandler("start", start))

app.run_polling()
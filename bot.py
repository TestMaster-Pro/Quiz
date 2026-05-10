import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import pytz
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    PollAnswerHandler,
)
from telegram.error import TelegramError

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Your bot token and group ID
BOT_TOKEN = "8400426353:AAGCQ_jO9p7X8byq1dXEI8QVK1GpFIVDPCY"
GROUP_ID = -1001831813306

# IST timezone
IST = pytz.timezone('Asia/Kolkata')

# Store active quizzes and user scores
active_quizzes: Dict[int, dict] = {}
user_scores: Dict[int, Dict[int, int]] = {}  # {chat_id: {user_id: score}}
quiz_tasks: Dict[int, asyncio.Task] = {}

# Flask app for keeping bot alive
app = Flask(__name__)

@app.route('/health')
def health_check():
    return 'Bot is running!', 200

@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates via webhook"""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        application.process_update(update)
    return "OK"

# Quiz settings
class QuizSettings:
    def __init__(self):
        self.interval = 30  # seconds between questions
        self.result_time = None  # IST time for results
        self.is_active = False
        self.current_question = 0
        self.questions = []
        self.start_time = None

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is admin"""
    user_id = update.effective_user.id
    try:
        chat_member = await context.bot.get_chat_member(
            chat_id=GROUP_ID,
            user_id=user_id
        )
        return chat_member.status in ['creator', 'administrator']
    except TelegramError:
        return False

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start quiz command - only for admins"""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Only admins can start quiz!")
        return
    
    chat_id = update.effective_chat.id
    
    if chat_id in active_quizzes and active_quizzes[chat_id].is_active:
        await update.message.reply_text("⚠️ A quiz is already running!")
        return
    
    # Initialize quiz settings
    active_quizzes[chat_id] = QuizSettings()
    user_scores[chat_id] = {}
    
    # Ask for interval
    keyboard = [
        [InlineKeyboardButton("15 seconds ⚡", callback_data="interval_15")],
        [InlineKeyboardButton("30 seconds 🕐", callback_data="interval_30")],
        [InlineKeyboardButton("45 seconds 🕑", callback_data="interval_45")],
        [InlineKeyboardButton("60 seconds 🕒", callback_data="interval_60")],
        [InlineKeyboardButton("Custom ⚙️", callback_data="interval_custom")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎯 *Quiz Setup - Step 1/2*\n\n"
        "⏱️ How many seconds between each question?\n"
        "Select an option or choose Custom:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks for quiz setup"""
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    
    if not await is_admin(update, context):
        await query.edit_message_text("❌ Only admins can configure quiz!")
        return
    
    if chat_id not in active_quizzes:
        await query.edit_message_text("❌ No active quiz setup found!")
        return
    
    quiz = active_quizzes[chat_id]
    data = query.data
    
    if data.startswith("interval_"):
        if data == "interval_custom":
            context.user_data['awaiting_custom_interval'] = True
            await query.edit_message_text(
                "⌨️ *Enter custom interval*\n\n"
                "Please type the number of seconds (e.g., 45):",
                parse_mode='Markdown'
            )
            return
        else:
            interval = int(data.split("_")[1])
            quiz.interval = interval
            
            # Ask for result time
            await ask_result_time(query)
    
    elif data.startswith("result_time_"):
        time_value = data.split("_")[2]
        
        if time_value == "custom":
            context.user_data['awaiting_custom_time'] = True
            await query.edit_message_text(
                "⌨️ *Enter result time in IST*\n\n"
                "Format: HH:MM (24-hour format)\n"
                "Example: 17:30 for 5:30 PM\n\n"
                "Or type 'now+X' where X is minutes from now\n"
                "Example: now+30 for results in 30 minutes",
                parse_mode='Markdown'
            )
            return
        else:
            await set_result_time(query, time_value)

async def ask_result_time(query):
    """Ask user for result time"""
    keyboard = [
        [InlineKeyboardButton("After 10 min ⏱️", callback_data="result_time_10min")],
        [InlineKeyboardButton("After 20 min ⏱️", callback_data="result_time_20min")],
        [InlineKeyboardButton("After 30 min ⏱️", callback_data="result_time_30min")],
        [InlineKeyboardButton("After 1 hour ⏰", callback_data="result_time_60min")],
        [InlineKeyboardButton("Specific IST Time 🕐", callback_data="result_time_custom")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎯 *Quiz Setup - Step 2/2*\n\n"
        "📊 When do you want results and leaderboard?\n"
        "Choose an option:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def set_result_time(query, time_value):
    """Set the result time for quiz"""
    chat_id = query.message.chat_id
    quiz = active_quizzes[chat_id]
    
    now_ist = datetime.now(IST)
    
    if time_value.endswith("min"):
        minutes = int(time_value.replace("min", ""))
        quiz.result_time = now_ist + timedelta(minutes=minutes)
    elif ":" in time_value:
        try:
            hour, minute = map(int, time_value.split(":"))
            quiz.result_time = now_ist.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if quiz.result_time < now_ist:
                quiz.result_time += timedelta(days=1)  # Next day if time passed
        except ValueError:
            await query.edit_message_text(
                "❌ Invalid time format! Please use HH:MM format."
            )
            return
    else:
        await query.edit_message_text("❌ Invalid time format!")
        return
    
    # Start the quiz
    await start_quiz_execution(chat_id, query)

async def start_quiz_execution(chat_id, query):
    """Execute the quiz"""
    quiz = active_quizzes[chat_id]
    
    # Load questions
    with open('questions.json', 'r') as f:
        questions_data = json.load(f)
    
    quiz.questions = questions_data['questions']
    quiz.is_active = True
    quiz.start_time = datetime.now(IST)
    
    # Format result time
    result_time_str = quiz.result_time.strftime("%I:%M %p IST")
    
    await query.edit_message_text(
        f"🎉 *Quiz Started!*\n\n"
        f"📝 Questions: {len(quiz.questions)}\n"
        f"⏱️ Interval: {quiz.interval} seconds\n"
        f"📊 Results at: {result_time_str}\n\n"
        f"Good luck everyone! 🤞",
        parse_mode='Markdown'
    )
    
    # Send first question announcement
    await query.message.chat.send_message(
        "📢 *Quiz begins in 5 seconds!*\n"
        "Get ready to answer...",
        parse_mode='Markdown'
    )
    
    # Start quiz task
    task = asyncio.create_task(run_quiz(chat_id, query.message.chat))
    quiz_tasks[chat_id] = task

async def handle_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom text input for interval/time"""
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    
    if 'awaiting_custom_interval' in context.user_data:
        try:
            interval = int(text)
            if interval < 5:
                await update.message.reply_text("❌ Interval must be at least 5 seconds!")
                return
            if interval > 300:
                await update.message.reply_text("❌ Interval cannot exceed 5 minutes (300 seconds)!")
                return
            
            active_quizzes[chat_id].interval = interval
            context.user_data.pop('awaiting_custom_interval')
            
            # Create fake query-like object
            keyboard = []
            await ask_result_time(update.message)
            
        except ValueError:
            await update.message.reply_text("❌ Please enter a valid number!")
    
    elif 'awaiting_custom_time' in context.user_data:
        now_ist = datetime.now(IST)
        
        if text.lower().startswith('now+'):
            try:
                minutes = int(text.split('+')[1])
                quiz = active_quizzes[chat_id]
                quiz.result_time = now_ist + timedelta(minutes=minutes)
                context.user_data.pop('awaiting_custom_time')
                await start_quiz_execution_after_custom(update, chat_id)
                return
            except (ValueError, IndexError):
                await update.message.reply_text("❌ Invalid format! Use: now+30")
                return
        
        try:
            hour, minute = map(int, text.split(":"))
            quiz = active_quizzes[chat_id]
            quiz.result_time = now_ist.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if quiz.result_time < now_ist:
                quiz.result_time += timedelta(days=1)
            
            context.user_data.pop('awaiting_custom_time')
            await start_quiz_execution_after_custom(update, chat_id)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format! Please use HH:MM (e.g., 17:30) or now+X"
            )

async def start_quiz_execution_after_custom(update, chat_id):
    """Start quiz after custom time input"""
    quiz = active_quizzes[chat_id]
    
    with open('questions.json', 'r') as f:
        questions_data = json.load(f)
    
    quiz.questions = questions_data['questions']
    quiz.is_active = True
    quiz.start_time = datetime.now(IST)
    
    result_time_str = quiz.result_time.strftime("%I:%M %p IST")
    
    await update.message.reply_text(
        f"🎉 *Quiz Started!*\n\n"
        f"📝 Questions: {len(quiz.questions)}\n"
        f"⏱️ Interval: {quiz.interval} seconds\n"
        f"📊 Results at: {result_time_str}\n\n"
        f"Good luck everyone! 🤞",
        parse_mode='Markdown'
    )
    
    # Start quiz task
    task = asyncio.create_task(run_quiz(chat_id, update.message.chat))
    quiz_tasks[chat_id] = task

async def run_quiz(chat_id, chat):
    """Run the quiz loop"""
    quiz = active_quizzes[chat_id]
    
    for i, question in enumerate(quiz.questions):
        if datetime.now(IST) >= quiz.result_time:
            break
        
        quiz.current_question = i + 1
        
        # Create poll
        try:
            message = await chat.send_poll(
                question=f"Q{i+1}: {question['question']}",
                options=question['options'],
                type='quiz',
                correct_option_id=question['correct_answer'],
                is_anonymous=False,
                open_period=quiz.interval
            )
            
            # Store poll message for tracking
            if 'polls' not in quiz.__dict__:
                quiz.polls = {}
            quiz.polls[message.poll.id] = {
                'question_id': question['id'],
                'points': question['points']
            }
            
            if i < len(quiz.questions) - 1:
                await asyncio.sleep(quiz.interval + 2)  # Extra 2 seconds buffer
                
        except TelegramError as e:
            logger.error(f"Error sending poll: {e}")
    
    # Schedule result display
    await schedule_results(chat_id, chat)

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll answers"""
    poll_answer = update.poll_answer
    user_id = poll_answer.user.id
    poll_id = poll_answer.poll_id
    
    # Find which quiz this poll belongs to
    for chat_id, quiz in active_quizzes.items():
        if quiz.is_active and hasattr(quiz, 'polls') and poll_id in quiz.polls:
            poll_info = quiz.polls[poll_id]
            
            if poll_answer.option_ids and len(poll_answer.option_ids) > 0:
                selected_option = poll_answer.option_ids[0]
                
                # Check if answer is correct
                question = quiz.questions[poll_info['question_id'] - 1]
                if selected_option == question['correct_answer']:
                    # Award points
                    if user_id not in user_scores[chat_id]:
                        user_scores[chat_id][user_id] = 0
                    user_scores[chat_id][user_id] += poll_info['points']
            break

async def schedule_results(chat_id, chat):
    """Schedule and display quiz results"""
    quiz = active_quizzes[chat_id]
    
    # Calculate wait time until result_time
    now_ist = datetime.now(IST)
    wait_seconds = (quiz.result_time - now_ist).total_seconds()
    
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
    
    # Display results
    await show_leaderboard(chat_id, chat)

async def show_leaderboard(chat_id, chat):
    """Display quiz leaderboard"""
    quiz = active_quizzes[chat_id]
    scores = user_scores.get(chat_id, {})
    
    if not scores:
        await chat.send_message(
            "📊 *Quiz Results*\n\n"
            "😔 No one participated in the quiz!",
            parse_mode='Markdown'
        )
        return
    
    # Sort users by score
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    # Build leaderboard message
    message = "🏆 *QUIZ LEADERBOARD* 🏆\n\n"
    message += f"📅 Date: {datetime.now(IST).strftime('%d-%m-%Y')}\n"
    message += f"⏰ Time: {datetime.now(IST).strftime('%I:%M %p IST')}\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    
    for rank, (user_id, score) in enumerate(sorted_scores, 1):
        try:
            # Get user info
            user = await chat.get_member(user_id)
            username = user.user.username or user.user.first_name
            
            if rank <= 3:
                medal = medals[rank - 1]
            else:
                medal = f"{rank}."
            
            message += f"{medal} *{username}*: {score} points\n"
        except TelegramError:
            message += f"{rank}. User {user_id}: {score} points\n"
    
    # Calculate total possible points
    total_points = sum(q['points'] for q in quiz.questions)
    message += f"\n📊 Total possible points: {total_points}"
    
    await chat.send_message(message, parse_mode='Markdown')
    
    # Cleanup
    quiz.is_active = False
    if chat_id in quiz_tasks:
        quiz_tasks[chat_id].cancel()
    
    # Save results to file
    save_results(chat_id, sorted_scores)

def save_results(chat_id, sorted_scores):
    """Save quiz results to JSON file"""
    results = {
        'date': datetime.now(IST).strftime('%Y-%m-%d'),
        'time': datetime.now(IST).strftime('%H:%M:%S'),
        'chat_id': chat_id,
        'scores': [{'user_id': uid, 'score': score} for uid, score in sorted_scores]
    }
    
    try:
        with open('quiz_results.json', 'a') as f:
            f.write(json.dumps(results) + '\n')
    except Exception as e:
        logger.error(f"Error saving results: {e}")

async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop an active quiz"""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Only admins can stop quiz!")
        return
    
    chat_id = update.effective_chat.id
    
    if chat_id not in active_quizzes or not active_quizzes[chat_id].is_active:
        await update.message.reply_text("❌ No active quiz running!")
        return
    
    # Cancel the quiz task
    if chat_id in quiz_tasks:
        quiz_tasks[chat_id].cancel()
    
    active_quizzes[chat_id].is_active = False
    await update.message.reply_text("🛑 Quiz has been stopped!")

async def quiz_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current quiz status"""
    chat_id = update.effective_chat.id
    
    if chat_id not in active_quizzes or not active_quizzes[chat_id].is_active:
        await update.message.reply_text("❌ No active quiz running!")
        return
    
    quiz = active_quizzes[chat_id]
    result_time_str = quiz.result_time.strftime("%I:%M %p IST")
    
    await update.message.reply_text(
        f"📊 *Quiz Status*\n\n"
        f"📝 Current question: {quiz.current_question}/{len(quiz.questions)}\n"
        f"⏱️ Interval: {quiz.interval} seconds\n"
        f"📊 Results at: {result_time_str}",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = (
        "🤖 *Quiz Bot Commands*\n\n"
        "*/startquiz* - Start a new quiz (Admin only)\n"
        "*/stopquiz* - Stop running quiz (Admin only)\n"
        "*/quizstatus* - Check quiz status\n"
        "*/help* - Show this help message\n\n"
        "📝 *How to play:*\n"
        "• Answer polls as they appear\n"
        "• Each correct answer gives points\n"
        "• Check leaderboard after quiz ends"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

def main():
    """Main function to run the bot"""
    global application
    
    # Create Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("startquiz", start_quiz))
    application.add_handler(CommandHandler("stopquiz", stop_quiz))
    application.add_handler(CommandHandler("quizstatus", quiz_status))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    
    # Handle text messages for custom input
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, 
        handle_custom_input
    ))
    
    # Start Flask app on a separate thread for health checks
    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)
    
    import threading
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    
    # Start bot
    print("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
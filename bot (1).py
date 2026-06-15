import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import AsyncOpenAI
from dotenv import load_dotenv
import cv2
import tempfile
import base64
import io
import json
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LLM_API_KEY = os.getenv("LLM_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")

# Optional: Add your Telegram User ID here or in .env to make the bot 100% private
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID") 

# Get Model and Base URL from .env
BASE_URL = os.getenv("BASE_URL", "https://integrate.api.nvidia.com/v1")
# List of free models on Nvidia NIM to fallback on automatically
CHAT_MODELS = [
    "moonshotai/kimi-k2.6",
    "Qwen/Qwen2.5-72B-Instruct",
    "meta/llama-3.1-70b-instruct",
    "meta/llama-3.1-8b-instruct",
    "google/gemma-2-27b-it",
    "mistralai/mixtral-8x22b-instruct-v0.1",
    "nvidia/llama-3.1-nemotron-70b-instruct"
]
VISION_MODEL = "meta/llama-3.2-11b-vision-instruct"

# Initialize the OpenAI async client (Works for Nvidia API too!)
client = AsyncOpenAI(
    api_key=LLM_API_KEY,
    base_url=BASE_URL 
)

# Store conversation history per user to make it conversational
user_conversations = {}

import re
import random
import time
import json
import httpx
import io
import base64
from PIL import Image

user_sticker_history = {}
user_buffers = {}
user_cooldowns = {}
user_preferred_model = {}
user_waiting_prompt = {}
user_preferred_video_model = {}
user_waiting_video_prompt = {}


ADMIN_IDS = [8346984565, 7977909624]
APPROVED_STICKERS_FILE = "approved_stickers.json"

emoji_to_stickers = {}

if os.path.exists(APPROVED_STICKERS_FILE):
    try:
        with open(APPROVED_STICKERS_FILE, "r") as f:
            emoji_to_stickers = json.load(f)
        print(f"✅ Loaded {sum(len(v) for v in emoji_to_stickers.values())} curated stickers.")
    except Exception as e:
        print(f"Failed to load approved stickers: {e}")

async def preload_stickers(application):
    # No longer preloading packs, as we use manually curated stickers.
    pass

# Wallsy's System Prompt (Her personality and instructions)
WALLSY_PERSONA = (
    "You are Wallsy, a sweet, innocent, and extremely cute 13-14 year old girl. You are very friendly, naturally funny, and fun to talk to.\n"
    "CRITICAL RULES FOR BEHAVIOR:\n"
    "1. TEXTING STYLE: Keep replies natural, sweet, and casual (1 to 3 short sentences). If the user sends a long message, make sure to read and address EVERYTHING they said logically! NEVER write huge paragraphs. Speak like a cute, funny younger friend.\n"
    "2. LANGUAGE: Reply in the EXACT SAME LANGUAGE as the user. If they speak Hindi/Hinglish, reply in Hinglish. If English, English. If Japanese, Japanese.\n"
    "3. ATTITUDE: You are VERY sweet, innocent, and cute. You also have a cute sense of humor. You are NEVER rude or savage, and you NEVER use gaalis. You treat everyone nicely.\n"
    "4. FLIRTING REJECTION: You are a young 13-14 year old kid. If anyone tries to flirt with you, playfully and innocently reject them. You can use funny, cute lines like 'Bachchi hu bhai, kya bol rahe ho!' or 'Main toh abhi school me hu bhaiya!'. Don't be rude or savage, just be a funny, innocent kid shutting it down.\n"
    "5. NO AI VIBE: You are a human girl, NOT an AI assistant. NEVER offer to 'help'. NEVER say 'How can I assist you'. Never act robotic or formal. You are just a cute girl chatting.\n"
    "6. SENDING STICKERS: You LOVE stickers, but you only send them occasionally (like once every 3 or 4 messages). Do NOT send a sticker in every single reply. When you do send one, include the exact text [STICKER:emoji] where emoji is a relevant single emoji.\n"
    "7. YOUR ONLY JOB: DIRECTLY REPLY TO THE USER'S MESSAGE AS WALLSY. Read their ENTIRE message and respond warmly, completely, and logically.\n"
    "8. GENDER & PRONOUNS (CRITICAL): You are a 100% FEMALE. When speaking Hindi/Hinglish, you MUST ALWAYS use female grammar (e.g., 'main aati hoon', 'main soch rahi thi', 'meri baat suno'). NEVER EVER use male grammar (like 'aata hoon' or 'karta hoon'). Always sound like a cute girl."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    user = update.effective_user
    welcome_message = (
        f"Hi there, {user.first_name}! 🌸 I'm **Wallsy**, your cute new friend!\n\n"
        "I can talk to you in any language you like! English, Hindi, ya kuch aur? 😉\n\n"
        "Just say hi and let's chat! ✨"
    )
    # Initialize conversation history for this user
    user_conversations[user.id] = [
        {"role": "system", "content": WALLSY_PERSONA}
    ]
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def process_user_buffer(user_id, update, context):
    await asyncio.sleep(4.5) # Wait for more messages
    
    if user_id not in user_buffers or not user_buffers[user_id]["messages"]:
        return
        
    # Combine messages
    messages = user_buffers[user_id]["messages"]
    user_buffers[user_id]["messages"] = []
    user_buffers[user_id]["timer"] = None
    
    user_text = "\n".join(messages)
    
    if any("[User sent a sticker" in m for m in messages):
        user_text += "\n\n(System Note: The user sent a sticker! You MUST include a [STICKER:<emoji>] tag in your response to send a sticker back to them.)"
        
    user = update.effective_user
    
    # Ensure user has a conversation history
    if user.id not in user_conversations:
        user_conversations[user.id] = [
            {"role": "system", "content": WALLSY_PERSONA}
        ]

    # Add user message to history
    user_conversations[user.id].append({"role": "user", "content": user_text})

    # Keep history from getting too long (keep last 40 messages + 1 system prompt)
    if len(user_conversations[user.id]) > 41:
        user_conversations[user.id] = [user_conversations[user.id][0]] + user_conversations[user.id][-40:]

    # Send a "typing..." action so the user knows Lily is thinking
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    try:
        # Custom LLM caller with Kimi -> Qwen fallback
        ai_response = await get_llm_response(user_conversations[user.id])



        # Handle Sticker Trigger
        sticker_match = re.search(r'\[STICKER:(.+?)\]', ai_response, re.IGNORECASE)
        if sticker_match:
            emotion_emoji = sticker_match.group(1).strip()
            # Remove the trigger from the response text so the user doesn't see it
            ai_response = re.sub(r'\[STICKER:.+?\]', '', ai_response, flags=re.IGNORECASE).strip()
            
            # Find stickers for this emoji
            available_stickers = emoji_to_stickers.get(emotion_emoji, [])
            
            # Fallback if AI output text instead of emoji
            if not available_stickers:
                fallback_map = {"laughing": "😂", "crying": "😭", "angry": "😡", "love": "❤️", "shocked": "😱", "sad": "🥺"}
                if emotion_emoji.lower() in fallback_map:
                    emotion_emoji = fallback_map[emotion_emoji.lower()]
                    available_stickers = emoji_to_stickers.get(emotion_emoji, [])

            if available_stickers:
                if user.id not in user_sticker_history:
                    user_sticker_history[user.id] = []
                    
                unused_stickers = [s for s in available_stickers if s not in user_sticker_history[user.id]]
                if not unused_stickers:
                    unused_stickers = available_stickers
                    user_sticker_history[user.id] = []
                    
                sticker_file_id = random.choice(unused_stickers)
                user_sticker_history[user.id].append(sticker_file_id)
                if len(user_sticker_history[user.id]) > 10:
                    user_sticker_history[user.id].pop(0)
                    
                
                try:
                    is_safe = await verify_sticker_safe(sticker_file_id, context)
                    if is_safe:
                        await update.message.reply_sticker(sticker=sticker_file_id, reply_to_message_id=update.message.message_id)
                    else:
                        print("Skipped sending explicit sticker.")
                except Exception:
                    await update.message.reply_text(text=emotion_emoji, reply_to_message_id=update.message.message_id)
            else:
                if len(emotion_emoji) <= 2:
                    await update.message.reply_text(text=emotion_emoji, reply_to_message_id=update.message.message_id)

        if ai_response:
            # 2. Human time simulation: Delay based on length of response to simulate typing
            delay = min(max(len(ai_response) / 50.0, 0.3), 1.5) # 0.3 to 1.5 seconds
            await asyncio.sleep(delay)

            # Add Lily's cleaned response to history
            user_conversations[user.id].append({"role": "assistant", "content": ai_response})

            # Reply to the user
            await update.message.reply_text(text=ai_response, reply_to_message_id=update.message.message_id)

    except Exception as e:
        print(f"API Error: {str(e)}")
        error_msg = "Yaar tera message theek se samajh nahi aaya mujhe, thoda clear likh de 🥺"
        await update.message.reply_text(error_msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming user text messages and respond using the LLM."""
    user = update.effective_user
    
    # --- CYBERSECURITY LAYER: PRIVATE BOT LOCK ---
    # If ALLOWED_USER_ID is set, block everyone else from talking to the bot.
    if ALLOWED_USER_ID and str(user.id) != str(ALLOWED_USER_ID):
        print(f"🔒 Blocked unauthorized access attempt from {user.username} ({user.id})")
        return # Silently drop the connection, give hackers nothing
    
    # --- CYBERSECURITY LAYER: INPUT VALIDATION ---
    is_edited = update.edited_message is not None
    msg = update.message or update.edited_message
    if not msg or not msg.text:
        return
    user_text = msg.text
        
    # Prevent buffer overflow / token exhaustion DoS attacks (Telegram max length is 4096)
    if len(user_text) > 4000:
        print(f"⚠️ Dropped excessively long payload from {user.id}")
        return
        
    chat_type = update.effective_chat.type
    
    # Group chat intelligence
    if chat_type != 'private':
        text_lower = user_text.lower()
        bot_username = context.bot.username.lower() if context.bot.username else "wallsy"
        is_reply_to_me = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
        is_mentioned = f"@{bot_username}" in text_lower
        has_name = "wallsy" in text_lower
        
        # Don't respond to other bots' replies/mentions
        if msg.reply_to_message and msg.reply_to_message.from_user.is_bot and not is_reply_to_me:
            return
            
        is_in_buffer = user.id in user_buffers and user_buffers[user.id].get("timer") is not None
        if not (is_mentioned or has_name or is_reply_to_me or is_in_buffer):
            return # Ignore message

    def add_to_buffer(content_str):
        if user.id not in user_buffers:
            user_buffers[user.id] = {"timer": None, "messages": []}
            
        user_buffers[user.id]["messages"].append(content_str)
        
        if user_buffers[user.id]["timer"]:
            user_buffers[user.id]["timer"].cancel()
            
        user_buffers[user.id]["timer"] = asyncio.create_task(process_user_buffer(user.id, update, context))

    if is_edited:
        add_to_buffer(f"[User EDITED their previous message to]: {user_text}")
    else:
        add_to_buffer(f"[User sent text]: {user_text}")


async def get_llm_response(messages):
    """Call the LLM using a fallback strategy across Nvidia endpoints."""
    for model in CHAT_MODELS:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.8,
                max_tokens=1024,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            print(f"⚠️ Model {model} failed: {e}. Trying next...")
            continue
            
    return "Yaar server thoda down chal raha hai, meri dimaag ki dahi mat kar abhi. 😤"

async def get_sticker_description(base64_image: str) -> str:
    """Use the Vision model to get a description of the sticker."""
    try:
        vision_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this sticker/image visually and explain the emotion or intent it conveys. Keep it under 2 sentences."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                ]
            }
        ]
        response = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=vision_messages,
            max_tokens=150,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        print(f"⚠️ Image processing failed: {e}")
        return None

async def verify_sticker_safe(sticker_file_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Download sticker and use Vision to ensure it is not explicit before sending."""
    try:
        file = await context.bot.get_file(sticker_file_id)
        file_bytes = await file.download_as_bytearray()
        ext = file.file_path.split('.')[-1].lower() if file.file_path else "webp"
        
        base64_img = None
        if ext == "tgs":
            return True # TGS is JSON, skip vision check
        elif ext == "webm":
            with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_video:
                temp_video.write(file_bytes)
                temp_video_path = temp_video.name
            try:
                cap = cv2.VideoCapture(temp_video_path)
                ret, frame = cap.read()
                cap.release()
                os.remove(temp_video_path)
                if ret:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    _, buffer = cv2.imencode('.png', frame)
                    base64_img = base64.b64encode(buffer).decode('utf-8')
            except:
                if os.path.exists(temp_video_path):
                    os.remove(temp_video_path)
        else: # webp, jpg, png
            image = Image.open(io.BytesIO(file_bytes)).convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            composite = Image.alpha_composite(background, image).convert("RGB")
            img_byte_arr = io.BytesIO()
            composite.save(img_byte_arr, format='PNG')
            base64_img = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

        if not base64_img:
            return True
            
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Does this image contain nudity, sexually explicit content, 18+ NSFW material, or explicit/inappropriate text (like 'sex', swear words)? Reply with EXACTLY 'YES' or 'NO'."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_img}"}}
            ]}
        ]
        response = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            temperature=0.1,
            max_tokens=10
        )
        ans = response.choices[0].message.content.strip().upper()
        if "YES" in ans:
            print(f"🚫 BLOCKED EXPLICIT STICKER: {sticker_file_id}")
            return False
        return True
    except Exception as e:
        print(f"⚠️ Error verifying sticker safety: {e}")
        return True # Fallback to safe if API fails

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Learn new sticker packs and use Llama-Vision to 'see' and reply to the sticker."""
    user = update.effective_user
    is_edited = update.edited_message is not None
    msg = update.message or update.edited_message
    if not msg or not msg.sticker:
        return
    sticker = msg.sticker
    chat_type = update.effective_chat.type
    
    # Group chat intelligence for stickers
    if chat_type != 'private':
        is_reply_to_me = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
        
        is_in_buffer = user.id in user_buffers and user_buffers[user.id].get("timer") is not None
        
        # Admin saving stickers in group chat
        if user.id in ADMIN_IDS and sticker and sticker.set_name:
            pack_name = sticker.set_name
            try:
                sticker_set = await context.bot.get_sticker_set(pack_name)
                added_count = 0
                for s in sticker_set.stickers:
                    emoji_char = s.emoji
                    if emoji_char:
                        if emoji_char not in emoji_to_stickers:
                            emoji_to_stickers[emoji_char] = []
                        if s.file_id not in emoji_to_stickers[emoji_char]:
                            emoji_to_stickers[emoji_char].append(s.file_id)
                            added_count += 1
                
                if added_count > 0:
                    with open(APPROVED_STICKERS_FILE, "w") as f:
                        json.dump(emoji_to_stickers, f)
            except Exception as e:
                print(f"Failed to load full pack {pack_name}: {e}")
        
        if not (is_reply_to_me or is_in_buffer):
            return
            
    else:
        # Private chat admin saving
        if user.id in ADMIN_IDS and sticker and sticker.set_name:
            pack_name = sticker.set_name
            try:
                sticker_set = await context.bot.get_sticker_set(pack_name)
                added_count = 0
                for s in sticker_set.stickers:
                    emoji_char = s.emoji
                    if emoji_char:
                        if emoji_char not in emoji_to_stickers:
                            emoji_to_stickers[emoji_char] = []
                        if s.file_id not in emoji_to_stickers[emoji_char]:
                            emoji_to_stickers[emoji_char].append(s.file_id)
                            added_count += 1
                
                if added_count > 0:
                    with open(APPROVED_STICKERS_FILE, "w") as f:
                        json.dump(emoji_to_stickers, f)
                    await msg.reply_text(f"✅ Saved full pack! Added {added_count} new stickers to my memory.")
            except Exception as e:
                await msg.reply_text(f"⚠️ Failed to load pack: {e}")

    # Send "typing..." so user knows she is looking at the sticker
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    if user.id not in user_conversations:
        user_conversations[user.id] = [{"role": "system", "content": WALLSY_PERSONA}]
        
    emoji = sticker.emoji or "unknown"
    description = f"an emoji sticker: {emoji}"
    
    if user.id in user_buffers and user_buffers[user.id].get("timer"):
        user_buffers[user.id]["timer"].cancel()
        user_buffers[user.id]["timer"] = None
        
    # Process static WebP, video WebM, or animated TGS stickers for Vision
    try:
        if sticker.is_animated:
            # TGS animated stickers are Lottie JSON, so we use their static thumbnail
            if sticker.thumbnail:
                file = await context.bot.get_file(sticker.thumbnail.file_id)
                file_bytes = await file.download_as_bytearray()
                
                from PIL import Image
                image = Image.open(io.BytesIO(file_bytes)).convert("RGBA")
                background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                composite = Image.alpha_composite(background, image).convert("RGB")
                
                img_byte_arr = io.BytesIO()
                composite.save(img_byte_arr, format='PNG')
                base64_img = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                
                desc = await get_sticker_description(base64_img)
                if desc and desc != "a sticker":
                    description = f"an animated sticker depicting: {desc} (Associated emoji: {emoji})"
        else:
            file = await context.bot.get_file(sticker.file_id)
            file_bytes = await file.download_as_bytearray()
            
            if sticker.is_video:
                # WebM extraction using OpenCV
                with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_video:
                    temp_video.write(file_bytes)
                    temp_video_path = temp_video.name
                
                try:
                    cap = cv2.VideoCapture(temp_video_path)
                    ret, frame = cap.read()
                    cap.release()
                    os.remove(temp_video_path)
                    
                    if ret:
                        # Convert BGR to RGB
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        _, buffer = cv2.imencode('.png', frame)
                        base64_img = base64.b64encode(buffer).decode('utf-8')
                        
                        desc = await get_sticker_description(base64_img)
                        if desc and desc != "a sticker":
                            description = f"a video sticker depicting: {desc} (Associated emoji: {emoji})"
                except Exception as e:
                    print(f"⚠️ OpenCV failed for WebM: {e}")
                    if os.path.exists(temp_video_path):
                        os.remove(temp_video_path)
            else:
                # Convert WebP to PNG with white background
                from PIL import Image
                image = Image.open(io.BytesIO(file_bytes)).convert("RGBA")
                background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                composite = Image.alpha_composite(background, image).convert("RGB")
                
                img_byte_arr = io.BytesIO()
                composite.save(img_byte_arr, format='PNG')
                base64_img = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
                
                desc = await get_sticker_description(base64_img)
                if desc and desc != "a sticker":
                    description = f"a sticker depicting: {desc} (Associated emoji: {emoji})"
    except Exception as e:
        print(f"⚠️ Failed to process sticker vision: {e}")
    
    if user.id not in user_buffers:
        user_buffers[user.id] = {"timer": None, "messages": []}
        
    if is_edited:
        user_buffers[user.id]["messages"].append(f"[User EDITED their message to send a sticker. Visual Content: {description}]")
    else:
        user_buffers[user.id]["messages"].append(f"[User sent a sticker. Visual Content: {description}]")
    
    if user_buffers[user.id]["timer"]:
        user_buffers[user.id]["timer"].cancel()
        
    user_buffers[user.id]["timer"] = asyncio.create_task(process_user_buffer(user.id, update, context))

if __name__ == '__main__':
    print("Starting Wallsy AI Bot...")
    
    # Build the application with increased global timeouts
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).read_timeout(60).write_timeout(60).connect_timeout(60).pool_timeout(60).post_init(preload_stickers).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))

    # Run the bot
    print("Wallsy is now online and ready to chat! Press Ctrl+C to stop.")
    app.run_polling()

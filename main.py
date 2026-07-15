import os
import logging
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from github_internships import GithubInternships
from database import SupabaseDatabase
import threading
from fastapi import FastAPI
import uvicorn
from urllib.parse import quote

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CATEGORY_COLORS = {
    "Software Engineering": discord.Color.blue(),
    "AI / ML": discord.Color.purple(),
    "Data Science": discord.Color.green(),
    "Quant": discord.Color.gold(),
    "Product": discord.Color.orange(),
    "Other": discord.Color.light_grey(),
}

handler = logging.FileHandler(filename="logs/discord.log", encoding="utf-8", mode="w")
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)
github_internships = GithubInternships()
supabase_db = SupabaseDatabase()

app = FastAPI()


@app.get("/")
def home():
    return {"status": "Discord bot running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)


async def send_internship(internship):
    channel = await bot.fetch_channel(CHANNEL_ID)

    company = internship["company_name"]
    title = internship["job_title"]
    location = internship["job_location"]
    category = internship["job_type"]
    url = internship["job_url"]

    embed = discord.Embed(
        title=f"🚀 {title}",
        url=url,
        description=f"### **{company}**",
        color=CATEGORY_COLORS[category],
        timestamp=discord.utils.utcnow(),
    )

    # Company logo
    company_domain = company.lower()
    company_domain = (
        company_domain.replace(" ", "")
        .replace(",", "")
        .replace(".", "")
        .replace("&", "")
        .replace("'", "")
    )

    embed.set_thumbnail(url=f"https://logo.clearbit.com/{company_domain}.com")

    embed.add_field(name="🏢 Company", value=f"**{company}**", inline=True)

    embed.add_field(name="📍 Location", value=f"```{location}```", inline=True)

    embed.add_field(name="💻 Category", value=f"```{category}```", inline=False)

    embed.add_field(
        name="🟢 Apply", value=f"**[Open Internship ↗]({url})**", inline=False
    )

    embed.add_field(
        name="📂 Source",
        value="[Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)",
        inline=True,
    )

    # Search links
    google = f"https://www.google.com/search?q={quote(company + ' careers')}"

    linkedin = (
        f"https://www.linkedin.com/search/results/companies/?keywords={quote(company)}"
    )

    embed.add_field(
        name="🔍 Research",
        value=(f"[Company Careers]({google}) • [LinkedIn]({linkedin})"),
        inline=True,
    )

    embed.set_footer(
        text="CS Internship Bot • Auto-updated every 15 minutes",
        icon_url=bot.user.display_avatar.url,
    )

    embed.set_author(
        name="✨ New Internship Found!", icon_url=bot.user.display_avatar.url
    )

    await channel.send(embed=embed)


@tasks.loop(minutes=15)
async def check_new_internships():
    github_internships.get_internships()
    internships = supabase_db.get_unsent_internships()

    for internship in internships:
        try:
            await send_internship(internship)
            supabase_db.mark_as_sent(internship["id"])

        except Exception as e:
            print(e)


@check_new_internships.before_loop
async def before_check():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    await bot.tree.sync()
    if not check_new_internships.is_running():
        check_new_internships.start()


if __name__ == "__main__":
    threading.Thread(target=run_web_server).start()

    bot.run(TOKEN, log_handler=handler, log_level=logging.INFO, reconnect=True)

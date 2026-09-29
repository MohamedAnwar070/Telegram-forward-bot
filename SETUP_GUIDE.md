# 🚀 Enhanced Telegram Forward Bot - Setup Guide

## Features Overview

### Core Features
- ✅ **Smart Forwarding** - Bot + User session fallback
- ✅ **Duplicate Detection** - Never forward same message twice
- ✅ **Custom Captions** - Dynamic templating with metadata
- ✅ **Advanced Filters** - Media type, keywords, size, time-based
- ✅ **Queue System** - Auto-retry failed forwards
- ✅ **Statistics** - Global & per-mapping analytics
- ✅ **Session Management** - Multiple user accounts
- ✅ **Backup/Restore** - Configuration protection
- ✅ **Health Monitoring** - Automated system checks
- ✅ **Webhook Notifications** - Real-time alerts

### New Features vs Original
1. **Session Management UI** - Add/remove/toggle user sessions
2. **Advanced Filtering** - Media types, keywords, file size, time windows
3. **Failed Message Queue** - Auto-retry with exponential backoff
4. **Per-Mapping Statistics** - Track performance per source
5. **Scheduled Tasks** - Auto-cleanup, health checks, queue retries
6. **Configuration Backup** - Disaster recovery
7. **Webhook Integration** - Discord/Slack notifications
8. **Better Logging** - File + console with rotation
9. **Pause/Resume** - Control forwarding without deleting
10. **Caption Preview** - Test templates before applying

---

## Installation

### 1. Prerequisites
```bash
# Python 3.9+
python --version

# MongoDB
# Install from https://www.mongodb.com/try/download/community
# Or use MongoDB Atlas (cloud)
```

### 2. Clone & Install
```bash
# Install dependencies
pip install -r requirements.txt

# Or use virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

### 3. Configuration

#### Get Telegram API Credentials
1. Go to https://my.telegram.org
2. Login with your phone number
3. Click "API Development Tools"
4. Create new application
5. Copy `api_id` and `api_hash`

#### Get Bot Token
1. Message @BotFather on Telegram
2. Send `/newbot`
3. Follow instructions
4. Copy bot token

#### Get Your User ID
1. Message @userinfobot on Telegram
2. Copy your ID

#### Generate Encryption Key
```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

#### Setup Environment
```bash
# Copy example env
cp .env.example .env

# Edit with your values
nano .env
```

### 4. MongoDB Setup

#### Local MongoDB
```bash
# Start MongoDB
mongod --dbpath /path/to/data

# Or use system service
sudo systemctl start mongodb
```

#### MongoDB Atlas (Cloud)
1. Create account at https://www.mongodb.com/atlas
2. Create free cluster
3. Get connection string
4. Add to `.env` as `MONGO_URI`

---

## Usage Guide

### First Steps

1. **Start the bot**
```bash
python forward_bot.py
```

2. **Add your first mapping**
```
/add -1001234567890 -1009876543210
```
- Source: Channel/group to monitor
- Target: Where to forward messages

3. **Set custom caption**
```
/caption -1001234567890 📁 {filename}

📊 Size: {size}
⏱ Duration: {duration}

{original_caption}
```

### Advanced Configuration

#### Add User Session (for private channels)
```bash
# Generate session string first
python -c "from pyrogram import Client; import os; Client('my_session', api_id=int(os.getenv('API_ID')), api_hash=os.getenv('API_HASH')).start()"

# Copy the printed session string
# Then add to bot:
/session_add my_account SESSION_STRING_HERE
```

#### Setup Filters

**Media Type Filter**
```
/filter_media -1001234567890 video,photo,document
```

**Keyword Filter**
```
/filter_keywords -1001234567890 include:tutorial,guide exclude:spam,ad
```

**File Size Limit**
```
/filter_size -1001234567890 50
```
(Max 50 MB)

**Time-Based Forwarding**
```
/filter_time -1001234567890 09:00-17:00
```
(Only forward 9 AM - 5 PM)

#### View Configuration
```
/info -1001234567890
/filter_show -1001234567890
/stats -1001234567890
```

### Maintenance

#### Backup Configuration
```
/backup
```
Save: `Backup ID: 507f1f77bcf86cd799439011`

#### Restore from Backup
```
/restore 507f1f77bcf86cd799439011
```

#### Clean Old Data
```
/clean
```
Removes duplicate records older than 30 days

#### Health Check
```
/health
```
Shows system status

---

## Caption Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `{caption}` | Cleaned caption (no URLs/mentions) | "Check this out" |
| `{original_caption}` | Full original caption | "@user Check this http://..." |
| `{filename}` | File name | "video_2024.mp4" |
| `{size}` | Human-readable file size | "25.3 MB" |
| `{duration}` | Video/audio duration | "05:30" |
| `{date}` | Current date | "2024-12-30" |
| `{time}` | Current time | "14:35:22" |
| `{source}` | Source channel name | "Tech News" |
| `{views}` | View count | "1234" |

---

## Command Reference

### Mapping Management
- `/add SOURCE_ID TARGET_ID` - Create forwarding rule
- `/list` - Show all mappings
- `/remove SOURCE_ID` - Delete mapping
- `/pause SOURCE_ID` - Pause forwarding
- `/resume SOURCE_ID` - Resume forwarding
- `/info SOURCE_ID` - Show detailed info

### Caption Control
- `/caption SOURCE_ID TEMPLATE` - Set caption template
- `/caption_preview SOURCE_ID` - Test caption

### Session Management
- `/session_add NAME SESSION` - Add user account
- `/session_list` - List sessions
- `/session_remove ID` - Delete session
- `/session_toggle ID` - Enable/disable

### Filters
- `/filter_media SOURCE_ID TYPES` - Media type filter
- `/filter_keywords SOURCE_ID include:x exclude:y` - Keyword filter
- `/filter_size SOURCE_ID MAX_MB` - File size limit
- `/filter_time SOURCE_ID HH:MM-HH:MM` - Time window
- `/filter_clear SOURCE_ID` - Remove all filters
- `/filter_show SOURCE_ID` - Display active filters

### Monitoring
- `/stats [SOURCE_ID]` - View statistics
- `/queue` - Show failed messages
- `/retry MESSAGE_ID` - Retry failed forward
- `/health` - System health status

### Maintenance
- `/backup` - Create configuration backup
- `/restore BACKUP_ID` - Restore configuration
- `/clean` - Clean old database records

---

## Troubleshooting

### Bot Can't Forward
**Problem:** "ChatWriteForbidden" error

**Solution:**
1. Make sure bot is admin in target channel
2. Add user session: `/session_add`
3. Bot will auto-fallback to user session

### Messages Not Forwarding
**Check:**
1. Is mapping enabled? `/info SOURCE_ID`
2. Are filters blocking? `/filter_show SOURCE_ID`
3. Check queue: `/queue`
4. View logs: `tail -f forward_bot.log`

### High Queue Size
**Problem:** Many failed messages accumulating

**Solution:**
1. Check target channel permissions
2. Review error logs
3. Add user session for private channels
4. Auto-retry runs every 30 minutes

### Database Connection Failed
**Problem:** Can't connect to MongoDB

**Solution:**
```bash
# Check MongoDB is running
mongod --dbpath /path/to/data

# Or check service
sudo systemctl status mongodb

# Test connection
mongo --eval "db.runCommand({ connectionStatus: 1 })"
```

---

## Performance Tips

### For High Volume Channels
1. **Use user sessions** - Avoid bot rate limits
2. **Enable filters** - Reduce unnecessary forwards
3. **Clean database regularly** - `/clean` weekly
4. **Monitor queue** - `/queue` and `/stats` daily

### Database Optimization
```javascript
// Create indexes in MongoDB
db.duplicates.createIndex({ "time": 1 }, { expireAfterSeconds: 2592000 })
db.queue.createIndex({ "created_at": 1, "retries": 1 })
db.mappings.createIndex({ "source": 1 })
```

### Memory Management
- Bot automatically cleans inactive clients
- Duplicate records expire after 30 days
- Failed messages cleaned after 7 days (3+ retries)

---

## Security Best Practices

1. **Never share your `.env` file**
2. **Use strong encryption key** (32+ chars)
3. **Restrict MongoDB access** (use authentication)
4. **Keep session strings encrypted** (automatic)
5. **Limit admin access** (ADMIN_ID only)
6. **Use webhook HTTPS** (if configured)
7. **Regular backups** (`/backup` weekly)

---

## Webhook Integration

### Discord Webhook
```bash
# In .env
WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK
```

### Slack Webhook
```bash
WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

Notifications sent for:
- Forward errors
- High queue size (>100)
- System health failures

---

## Deployment

### Systemd Service (Linux)
```ini
[Unit]
Description=Telegram Forward Bot
After=network.target mongodb.service

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/bot
ExecStart=/path/to/venv/bin/python forward_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable forward-bot
sudo systemctl start forward-bot
sudo systemctl status forward-bot
```

### Docker
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "forward_bot.py"]
```

```bash
docker build -t forward-bot .
docker run -d --name forward-bot --env-file .env forward-bot
```

---

## Support & Contributing

### Logs Location
- Console output
- `forward_bot.log` file

### Common Issues
- Check GitHub issues
- Review logs
- Test with `/health`

### Feature Requests
Create detailed issue with:
- Use case description
- Expected behavior
- Example commands

---

## License

MIT License - Free to use and modify

---

## Changelog

### v2.0.0 (Enhanced)
- ✅ Session management UI
- ✅ Advanced filtering system
- ✅ Failed message queue
- ✅ Per-mapping statistics
- ✅ Scheduled maintenance tasks
- ✅ Configuration backup/restore
- ✅ Webhook notifications
- ✅ Health monitoring
- ✅ Pause/resume mappings
- ✅ Caption preview

### v1.0.0 (Original)
- Basic forwarding
- Duplicate detection
- Simple caption templates
- Admin commands

---

**Happy Forwarding! 🚀**
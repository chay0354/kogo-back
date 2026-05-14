# קוגומלו - Backend API

Django REST Framework backend for the Kogomalo class management system.

## Setup

### 1. Create Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Variables

Create a `.env` file in the backend directory with the following:

```env
# Django Settings
DEBUG=True
SECRET_KEY=your-secret-key-here

# Database (required): Supabase Postgres URI from Dashboard → Settings → Database.
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@db.YOUR_PROJECT_REF.supabase.co:5432/postgres
# CORS
CORS_ALLOWED_ORIGINS=http://localhost:3000

# Tranzila Payment Gateway (REQUIRED for payment processing)
# Get these credentials from your Tranzila account dashboard
TRANZILA_TERMINAL=your-terminal-id
TRANZILA_TOKEN_TERMINAL=your-token-terminal-id
TRANZILA_SUPPLIER=your-supplier-id
TRANZILA_PUBLIC_KEY=your-tranzila-public-key
TRANZILA_SECRET_KEY=your-tranzila-secret-key
TRANZILA_WEBHOOK_SECRET=your-webhook-secret
TRANZILA_API_BASE_URL=https://api.tranzila.com
TRANZILA_BASE_URL=https://direct.tranzila.com
TRANZILA_ENVIRONMENT=development

# Celery (optional - for background tasks)
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
```

### 4. Database Setup

Supabase hosts PostgreSQL. Set `DATABASE_URL` in `.env`, then:

```bash
python manage.py migrate
python manage.py create_manager --email you@example.com --password your-password --superuser
```

### 5. Run Development Server

```bash
python manage.py runserver
```

The API will be available at `http://localhost:8000`

## Tranzila Payment Integration

The system integrates with Tranzila payment gateway for processing payments and recurring subscriptions.

### Getting Tranzila Credentials

1. **Log into your Tranzila account** at https://tranzila.com
2. Navigate to **Settings → API Credentials**
3. You'll need the following credentials:
   - **Terminal ID** (TRANZILA_TERMINAL) - Main terminal for iframe payments
   - **Token Terminal ID** (TRANZILA_TOKEN_TERMINAL) - For REST API token charges
   - **Supplier ID** (TRANZILA_SUPPLIER) - Your merchant ID
   - **Public Key** (TRANZILA_PUBLIC_KEY) - For REST API authentication
   - **Secret Key** (TRANZILA_SECRET_KEY) - For REST API authentication
   - **Webhook Secret** (TRANZILA_WEBHOOK_SECRET) - For webhook signature verification

### Development Setup (localhost)

For local development, you need to expose your backend to the internet so Tranzila can send webhooks:

#### **Using Cloudflare Tunnel (Recommended):**

1. **Install Cloudflare Tunnel:**
   ```bash
   # Download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
   ```

2. **Start the tunnel** (in a separate terminal):
   ```bash
   cloudflared tunnel --url localhost:8000
   ```
   
   This will output a URL like: `https://random-words.trycloudflare.com`

3. **Configure Backend** - Add to your `.env` file:
   ```env
   ALLOWED_HOSTS=localhost,127.0.0.1,random-words.trycloudflare.com
   ```
   
   Or the tunnel domain is already configured in `settings.py` to allow all `.trycloudflare.com` domains.

4. **Configure Frontend** - Create `frontend/.env.local`:
   ```env
   NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1
   NEXT_PUBLIC_WEBHOOK_BASE_URL=https://random-words.trycloudflare.com/api/v1
   ```

5. **Restart frontend** after creating `.env.local`:
   ```bash
   npm run dev
   ```

#### **Testing Webhooks:**

1. Initiate a payment through the frontend
2. Check browser console for the callback URL being sent
3. Complete the payment in Tranzila
4. Check Django backend logs for: `🔔 WEBHOOK RECEIVED FROM TRANZILA`

### Production Setup

For production deployment, configure these environment variables on your hosting platform:

#### **1. Set Environment Variables**

**On your hosting platform** (Heroku, Railway, AWS, etc.), set:

```env
# Production Django Settings
DEBUG=False
SECRET_KEY=<strong-random-secret-key>
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com

# Database (use your production database URL)
DATABASE_URL=postgresql://user:password@host:port/dbname

# Tranzila Production Credentials
TRANZILA_TERMINAL=<your-production-terminal>
TRANZILA_TOKEN_TERMINAL=<your-production-token-terminal>
TRANZILA_SUPPLIER=<your-production-supplier>
TRANZILA_PUBLIC_KEY=<your-production-public-key>
TRANZILA_SECRET_KEY=<your-production-secret-key>
TRANZILA_WEBHOOK_SECRET=<your-production-webhook-secret>
TRANZILA_ENVIRONMENT=production

# Production URLs
TRANZILA_API_BASE_URL=https://api.tranzila.com
TRANZILA_BASE_URL=https://direct.tranzila.com

# Frontend URL (for webhook configuration)
FRONTEND_URL=https://yourdomain.com
```

#### **2. Configure Frontend Production Environment**

Create `frontend/.env.production` or set in your hosting platform:

```env
NEXT_PUBLIC_API_URL=https://yourdomain.com/api/v1
NEXT_PUBLIC_WEBHOOK_BASE_URL=https://yourdomain.com/api/v1
```

#### **3. Configure Tranzila Dashboard**

In your Tranzila account dashboard:

1. **Set Webhook URL:**
   - Go to Settings → Webhooks
   - Set webhook URL to: `https://yourdomain.com/api/v1/customers/payments/webhook/`
   - Enable webhook notifications for payment events

2. **Verify SSL Certificate:**
   - Ensure your production domain has a valid SSL certificate (Tranzila requires HTTPS)

3. **Test in Tranzila Sandbox** (if available):
   - Use test credentials before going live
   - Verify webhook delivery works

#### **4. Security Checklist**

- ✅ **Never commit `.env` files** to Git (already in `.gitignore`)
- ✅ **Use strong SECRET_KEY** in production (generate with `python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'`)
- ✅ **Set DEBUG=False** in production
- ✅ **Use HTTPS only** for production (required by Tranzila)
- ✅ **Rotate API keys** periodically
- ✅ **Monitor webhook failures** in Django logs
- ✅ **Keep TRANZILA_SECRET_KEY secure** - never expose in frontend code

#### **5. Webhook Endpoint**

The webhook endpoint is:
```
POST https://yourdomain.com/api/v1/customers/payments/webhook/
```

This endpoint:
- ✅ Is **public** (no authentication required)
- ✅ Validates webhook signature using `TRANZILA_WEBHOOK_SECRET`
- ✅ Is **CSRF exempt** (required for external webhooks)
- ✅ Processes payment confirmations automatically
- ✅ Updates child subscription status and enrollment

#### **6. Monitoring Webhooks**

Check Django logs for webhook activity:
```bash
# Look for these log messages:
🔔 WEBHOOK RECEIVED FROM TRANZILA
Method: POST
Path: /api/v1/customers/payments/webhook/
```

#### **7. Common Issues**

| Issue | Solution |
|-------|----------|
| "TRANZILA_PUBLIC_KEY not configured" | Set the environment variable in `.env` or hosting platform |
| Webhooks not received | Verify webhook URL is publicly accessible with HTTPS |
| "Invalid webhook signature" | Check `TRANZILA_WEBHOOK_SECRET` matches Tranzila dashboard |
| 403 Forbidden from Tranzila | Ensure `ALLOWED_HOSTS` includes your domain |

## Celery (Automated Tasks)

The system uses Celery for automated snapshot management:
- **Nightly Refresh**: Updates current month snapshots at 2:00 AM
- **Monthly Finalization**: Auto-finalizes previous month on the 1st at 3:00 AM

### Development Setup

1. **Start Redis** (message broker):
```bash
# Windows: Download from https://github.com/microsoftarchive/redis/releases
# Linux/Mac: sudo apt-get install redis-server || brew install redis
redis-server
```

2. **Start Celery Worker** (in separate terminal):
```bash
python -m celery -A config worker --loglevel=info --pool=solo
```

3. **Start Celery Beat** (in separate terminal):
```bash
python -m celery -A config beat --loglevel=info
```

### Production Setup

Use a process manager to run Celery as background services:

**Docker (Recommended):**
```yaml
celery-worker:
  command: celery -A config worker --loglevel=info
  depends_on: [redis]

celery-beat:
  command: celery -A config beat --loglevel=info
  depends_on: [redis]
```

**Systemd (Linux):**
```bash
# Create service files in /etc/systemd/system/
sudo systemctl enable celery-worker celery-beat
sudo systemctl start celery-worker celery-beat
```

**Supervisor:**
```bash
# Add configuration to /etc/supervisor/conf.d/celery.conf
supervisorctl start celery-worker celery-beat
```

## API Endpoints

All endpoints are prefixed with `/api/v1/`

- `/api/v1/branches/` - Branches management
- `/api/v1/instructors/` - Instructors management
- `/api/v1/courses/` - Courses catalog
- `/api/v1/customers/` - Customers management
- `/api/v1/enrollments/` - Enrollments management
- `/api/v1/schedule/` - Schedule view

## Project Structure

```
backend/
├── config/              # Django settings and main URLs
├── apps/
│   ├── core/           # Shared utilities and base models
│   ├── branches/       # Branches module
│   ├── instructors/    # Instructors module
│   ├── courses/        # Courses module
│   ├── customers/      # Customers module
│   ├── enrollments/    # Enrollments module
│   └── scheduling/     # Scheduling module
└── manage.py
```


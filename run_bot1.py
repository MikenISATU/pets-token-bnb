import os
os.environ["PORT"] = "8080"
os.environ["TELEGRAM_BOT_TOKEN"] = "7347310243:AAEp5T0YmOu0EYcDiF-hLIUthPAoz6lG-P0"
os.environ["BSCSCAN_API_KEY"] = "N4IZ37UXDV8985HCZZFQGHCFFHMSNYUKHQ"
os.environ["CLOUDINARY_CLOUD_NAME"] = "da4k3yxhu"
os.environ["RENDER_URL"] = "https://pets-tracker-1-2.onrender.com"  # Update after deployment
os.environ["ADMIN_CHAT_ID"] = "1888498588"
os.environ["PETS_BSC_ADDRESS"] = "0x2466858ab5edad0bb597fe9f008f568b00d25fe3"
from main import app
import uvicorn
uvicorn.run(app, host="0.0.0.0", port=8080)
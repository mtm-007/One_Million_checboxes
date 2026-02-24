from argparse import Action
from cgitb import text
from tkinter import Button
from turtle import ht
from click import style
from fasthtml import respond
import fasthtml.common as fh
#import asyncppg
import base64
import bcrypt
from fasthtml.core import RedirectResponse
from fasthtml.js import Form, Input
import jwt
from datetime import datetime, timedelta
from pathlib import Path
import os
from dataclasses import dataclass
import httpx
from typing import Optional

JWT_SECRET = os.getenv("")

hdr= css
app, rt = fh.fast_app(hrds = hdr)

async def init_db():
    global dp_pool
    #db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)

#helper functions
def create_token(user_id: str, email:str) -> str:
    payload = {
        'user_id': user_id,
        'email' : email,
        'exp': datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def verify_token(token: str) ->Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithm=['HS256'])
    except:
        return None

def get_current_user(request):
    """Get current user from cookie"""
    token = request.cookies.get('auth_token')
    if not token:
        return None
    payload = verify_token(token)
    return payload if payload else None

#middleware
@app.before_request
async def add_user_to_request(request, call_next):
    request.state.user = get_current_user(request)
    return await call_next(request)

@rt('/')
async def get(request):
    if request.state.user:
        return RedirectResponse('/dashboard')

    return fh.Titled("Photo AI - Register",
        fh.Div(cls="container")(
            fh.Div(cls="card", style="max-width: 400px; margin: 4rem auto;")(
                fh.H1("Create Account", style="text-align: center; margin-bottom: 2rem;"),
                fh.Form(action = "/api/register", method="POST")(
                    fh.Input(type="text", name="name", placeholder="Name", cls="input",required=True),
                    fh.Input(type="password", name="password", placeholder="Password", cls="input",required=True),
                    fh.Input(type="text", name="name", placeholder="Name", cls="input",required=True),
                    fh.Button("Create Account", type="submit", cls="btn btn-primary", style="width: 100%;"),
                ),
                fh.P(style="text-align:center; margin-top: 1rem;")(
                    "Already have an account? ",
                    fh.A("Sign in", href="/login", style="color: #a78bfa;")
    ))))

@rt('/api/register')
async def post(request, name: str, email: str, password: str):
    try:
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        #create user
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow(
                'INSERT INTO users (name, email, password_hash, credits) VALUES ($1, $2, $3, $4) RETURNING id, email, name, credits',
                name, email, password_hash, 50
            )
        #create token
        token = create_token(str(user['id']), user['email'])
        #set cookie and redirect
        response = RedirectResponse('/onboarding', status_code=303)
        response.set_cookie('auth_toekn', token, httponly=True, max_age=604800)
        return response
    except Exception as e:
        return fh.Titled("Error",
            fh.Div(cls="container")(
                fh.Div(cls="card error")(
                    fh.P(f"Registration failed: {str(e)}"),
                    fh.A("Try again", href="/register", cls="btn btn-primary")
        )))
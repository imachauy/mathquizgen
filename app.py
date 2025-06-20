from fastapi import FastAPI, Request, Depends, status, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Mount
from lti import validate_lti_request
from fastapi.responses import JSONResponse
from gradio_app import gradio_app
from fastapi import APIRouter

import os
import json
from typing import Annotated
import datetime, zoneinfo

from api import models
from api.connection import *

from functools import wraps
from typing import Dict, Optional
import asyncio
import time, pytz


from dotenv import load_dotenv

load_dotenv()

def convert_to_japan_time(dt):
    """
    Converts a given datetime object to Japan time zone (Asia/Tokyo).

    Parameters:
        dt (datetime): The datetime object to be converted.

    Returns:
        datetime: The converted datetime in Japan time zone.
    """
    # Define the Japan time zone
    japan_tz = pytz.timezone('Asia/Tokyo')
    
    # If the datetime is naive, assume it is in UTC and localize it
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    
    # Convert to Japan time zone
    return dt.astimezone(japan_tz)

class EndpointConcurrencyControl:
    def __init__(self):
        # Store semaphores for each endpoint
        self.endpoint_semaphores: Dict[str, asyncio.Semaphore] = {}
        # Store active requests for each endpoint-client combination
        self.active_requests: Dict[str, Dict[str, float]] = {}
    
    def limit_concurrency(self, max_concurrent: int = 40, per_client: bool = True):
        def decorator(func):
            # Create a unique key for this endpoint
            endpoint_key = f"{func.__module__}.{func.__name__}"
            
            # Initialize semaphore and active requests tracking for this endpoint
            self.endpoint_semaphores[endpoint_key] = asyncio.Semaphore(max_concurrent)
            self.active_requests[endpoint_key] = {}
            
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # Extract request object from args or kwargs
                request = next((arg for arg in args if isinstance(arg, Request)), 
                             kwargs.get('request'))
                
                if not request:
                    raise HTTPException(
                        status_code=500,
                        detail="Request object not found in endpoint arguments"
                    )
                
                client_id = request.client.host if per_client else "global"
                request_key = f"{endpoint_key}:{client_id}"
                
                # Check if client already has an active request for this endpoint
                if per_client and client_id in self.active_requests[endpoint_key]:
                    last_request_time = self.active_requests[endpoint_key][client_id]
                    while time.time() - last_request_time < 1:  # 1 second cooldown
                        asyncio.sleep(1)
                
                try:
                    async with self.endpoint_semaphores[endpoint_key]:
                        if per_client:
                            self.active_requests[endpoint_key][client_id] = time.time()
                        response = await func(*args, **kwargs)
                        return response
                finally:
                    if per_client and client_id in self.active_requests[endpoint_key]:
                        del self.active_requests[endpoint_key][client_id]
            
            return wrapper
        return decorator

# Initialize the concurrency control
concurrency_control = EndpointConcurrencyControl()

app = FastAPI(
    # root_path="/avery",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    title="MathQuizGen",
)

router = APIRouter()

app.mount("/static", StaticFiles(directory="static"), name="static")
@router.on_event("shutdown")
async def shutdown_client():
    await http_client.aclose()

BACKEND_URL = os.getenv("BACKEND_URL2")

origins = [
    BACKEND_URL,
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=os.environ.get('SECRET_KEY'))

templates = Jinja2Templates(directory="templates")

MAX_GENERATION = int(os.getenv("MAX_GENERATION", 5))

@router.route("/login", methods=["GET", "POST"])
@concurrency_control.limit_concurrency(max_concurrent=40, per_client=True)
async def login_form(request: Request):
    if request.method == "POST":
        form_data = await request.form()
        response = await get_access_token_from_backend(form_data=models.UserLogin(**form_data))

        # 認証失敗ならここで返す！
        if response.status_code != 200:
            return templates.TemplateResponse("login_form.html", {"request": request, "error": response.json()})

        # 認証成功したらtoken取り出し
        token = models.Token(**json.loads(response.json()))
        request.session["token"] = token.model_dump()
        request.session["username"] = form_data["username"]
        request.session["roles"] = "instructor" if form_data["username"] == "admin" else "student"
        request.session["program"] = form_data.get("program", "inlab_test")

        return RedirectResponse(url='/mathgen/ui', status_code=status.HTTP_303_SEE_OTHER)

    # GETのとき
    return templates.TemplateResponse("login_form.html", {"request": request})

'''
@router.route("/login", methods=["GET", "POST"])
@concurrency_control.limit_concurrency(max_concurrent=40, per_client=True)
async def login_form(request: Request):
    if request.method == "POST":
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        # 仮ログイン：適当なユーザー名とパスワードでも通す
        if username and password:
            # 仮トークンを強制発行
            token = {"access_token": "dummy_token"}
            request.session["token"] = token
            request.session["username"] = username
            request.session["roles"] = "student"
            request.session["program"] = "inlab_test"

            # ログイン成功したらGradioに飛ばす
            return RedirectResponse(url='/mathgen/ui', status_code=status.HTTP_303_SEE_OTHER)
        else:
            # 入力なければログイン画面に戻す
            return templates.TemplateResponse("login_form.html", {"request": request, "error": "Invalid login"})

    # GETリクエスト時はログイン画面を表示
    return templates.TemplateResponse("login_form.html", {"request": request})
'''
    
@router.route('/lti/login',methods=["POST"])
@concurrency_control.limit_concurrency(max_concurrent=40, per_client=True)
async def lti_login(request: Request):
    valid = await validate_lti_request(request)
    if not valid:
        return {'error': 'Invalid LTI request'} 
    
    # Extracting additional fields from the form data
    form_data = await request.form()

    user_id = form_data.get('user_id')
    oauth_consumer_key = form_data.get('oauth_consumer_key')

    if user_id:
        school = "School not provided"
        if oauth_consumer_key == "saikyo_consumer_key":
            school = "saikyo"
        elif oauth_consumer_key == "hikone_consumer_key":
            school = "hikone"
        elif oauth_consumer_key == "lms_consumer_key":
            school = "lms"

        if "instructor" in form_data.get('roles', '').lower():
            role = "instructor"
        else:
            role = "student"

        username = form_data.get('ext_user_username')
        user_login = models.UserLti(
            user_id=form_data.get('user_id'),
            username=username,
            display_name=form_data.get('lis_person_name_full', 'Unknown User'),
            roles=role,
            email=form_data.get('lis_person_contact_email_primary', ''),
            school=school,
        )

        response = await get_access_token_from_backend_lti(user=user_login)
        
        if not hasattr(response, 'status_code'):
            token = response
        elif response.status_code == 400:
            # Create user
            response = await create_user_lti(newuser=user_login)
            token = await get_access_token_from_backend_lti(user=user_login)
        else:
            response.raise_for_status()
        
        request.session["school"] = school
        request.session["token"] = token.model_dump()
        request.session["username"] = username
        request.session["roles"] = role
        request.session["program"] = form_data.get('custom_program', 'none')
        return RedirectResponse(url='/mathgen/ui', status_code=status.HTTP_303_SEE_OTHER)

    raise HTTPException(status_code=500, detail="Failed to login")

@router.route('/logout')
@concurrency_control.limit_concurrency(max_concurrent=40, per_client=True)
async def logout(request: Request):

    school = request.session.pop('school', None)

    token = request.session.pop('token', None)

    username = request.session.pop('username', None)

    program = request.session.pop('program', None)

    role = request.session.pop('roles', None)

    if school == "saikyo":
        return RedirectResponse(url='https://sk.let.media.kyoto-u.ac.jp')
    elif school == "hikone":
        return RedirectResponse(url='https://leaf02.uchida.co.jp/moodle/')
    elif school == "lms":
        return RedirectResponse(url='https://lms.let.media.kyoto-u.ac.jp/moodle/')
    else:
        return RedirectResponse(url='/mathgen/login')

@router.post("/token")
def token(request: Request):
    return request.session["token"]["access_token"]

@router.get("/")
async def redirect_page(request: Request):
    if "token" not in request.session:
        try:
            response = await read_leaderboard(request)
            assert response.status_code == 401
        except:
            return RedirectResponse(url="/mathgen/logout", status_code=status.HTTP_303_SEE_OTHER)
    if "leaderboard_id" in request.session:
        request.session.pop("leaderboard_id")
    return RedirectResponse(url="/mathgen/ui", status_code=status.HTTP_303_SEE_OTHER)

@app.exception_handler(Exception)
async def exception_handler(request: Request, exc: Exception):

    return RedirectResponse(url="/mathgen/logout", status_code=status.HTTP_303_SEE_OTHER)

def get_root_url(
    request: Request, route_path: str, root_path: Optional[str] = None
):
    # print(f"route_path: {route_path}\nroot_path: {root_path}\nrequest: {request.url if hasattr(request, 'url') else None}")
    root_path = root_path or request.scope.get("root_path", "")
    return root_path

@router.get('/routes')
def get_mounted_apps():
    routes = []
    for route in app.routes:
        methods = ', '.join(route.methods) if hasattr(route, 'methods') else 'No methods'
        routes.append({"path": route.path, "name": getattr(route, 'name', 'No name'), "methods": methods})

        if isinstance(route, Mount):
                routes.append({"path": route.path, "name": route.name, "app": str(route.app), "root_path":route.root_path if hasattr(route, 'root_path') else 'No root path'})

    return JSONResponse(content={"mounted_routes": routes})

@router.get("/go-to-gradio")
async def go_to_gradio():
    return RedirectResponse(url="/mathgen/ui")  # Gradioが動いてるポート

app.include_router(router, prefix="/mathgen")

app.mount("/mathgen/ui", gradio_app)
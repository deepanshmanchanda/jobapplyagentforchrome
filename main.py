from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class User(BaseModel):
    name: str
    email: str
    age : int


@app.post("/users")
def create_user(user: User):
    return user

@app.get("/")
def home():
    return {"message": "Hello, from API"}

@app.get("/contact")
def contact():
    return {"message": "Contact us at deepanshmanchanda@gmail.com"}

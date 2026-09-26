# Real Google Chrome + Xvfb come with this base image: the scraper keeps one
# Chrome window on google.com/travel/flights to sign its requests.
FROM chetan1111/botasaurus:chrome-151

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN python -m pip install --no-cache-dir -r requirements.txt

RUN mkdir app
WORKDIR /app
COPY . /app

EXPOSE 8000

CMD ["python", "run.py"]

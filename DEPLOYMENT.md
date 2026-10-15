# Deploying CareQueue

These are the same steps as NexusAI: push to GitHub, let CI and CD build the image, and run it on Render's free tier. It takes about 15 minutes.

## How the pipeline works

**CI** runs on every push and pull request. It runs:
- the linter;
- the tests against real PostgreSQL, Redis and RabbitMQ;
- a short simulation that fails if the waitlist stops helping;
- the React build;
- a Docker build that gets started and called.

**CD** runs only when CI passes on `main`. It publishes the Docker image to GitHub Container Registry (GHCR), tells Render to deploy that exact image, and waits until the live site reports the new commit. Until Render is connected, it publishes the image and skips the deploy.

## 1. Push to GitHub

Create an empty **public** repository called `carequeue`, then run this from the project folder:

```bash
git init
git add .
git status            # .env, .venv and node_modules must NOT be listed
git commit -m "CareQueue: clinic scheduling with patient and doctor portals"
git branch -M main
git remote add origin https://github.com/santhoshkumaar160302-ship-it/carequeue.git
git push -u origin main
```

Watch the **Actions** tab. CI takes about 5 minutes, then CD publishes the image.

## 2. Make the image public

The first CD run creates `ghcr.io/santhoshkumaar160302-ship-it/carequeue`. New packages start private, so open https://github.com/users/santhoshkumaar160302-ship-it/packages/container/carequeue/settings, scroll down to **Danger Zone**, click **Change visibility** and choose **Public**.

## 3. Create the service on Render

1. Go to https://dashboard.render.com/web/new and choose **Existing Image**.
2. For the image URL, enter `ghcr.io/santhoshkumaar160302-ship-it/carequeue:latest`.
3. Name it `carequeue` and pick the **Free** instance type.
4. Under **Advanced**, set the **Health Check Path** to `/api/health`.
5. Add this environment variable:

| Key | Value |
| --- | --- |
| `NEXUSAI_URL` | `https://nexusai-latest.onrender.com` |

That's all it needs. With no database or Redis set, it uses SQLite and an in-memory cache and queue, and creates a fresh demo clinic every time it starts.

## 4. Connect Render to GitHub

```bash
gh variable set DEPLOY_URL --body "https://<your-service>.onrender.com"
gh secret set RENDER_DEPLOY_HOOK_URL     # paste the Deploy Hook URL from Render → Settings
```

From then on, every push to `main` that passes CI goes live automatically.

## What to expect on the free tier

- **The demo resets.** Data lives in the container, so a restart or deploy brings back a fresh demo clinic. That suits a demo. For real use, set `DATABASE_URL` to a managed PostgreSQL, and `REDIS_URL` and `RABBITMQ_URL` to hosted services, then run `python -m carequeue.worker` as a separate background worker with `CQ_BACKGROUND_WORKER=0` on the web service.
- **The first visit is slow.** Free instances sleep after 15 minutes without traffic and take about a minute to wake up.
- **The assistant might fall back to rules.** If NexusAI is also asleep, the first request may time out and use keyword rules; the next one will use the LLM.
- **Anyone can use the demo accounts.** The sign-in page has one-click buttons for a demo patient and a demo doctor, and all the data is made up. For anything real, set `CQ_DEMO_LOGINS=0` to remove those buttons and turn on a persistent database. People can still register as patients; doctor accounts are created by the clinic.
- **Signing in uses a cookie.** It's marked `Secure` automatically when the site is served over HTTPS, as it is on Render.

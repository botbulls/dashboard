# Servers
!!! note
    An external server setup is recommended over one in your home because of speed, reliability and location. This guide is going to assume you're running Debian 10+

!!! tip
    This is my referral code for vultr which gives $100 free credit for 1 month: https://www.vultr.com/?ref=9034865-8H

## Server setup
Login using `root@IP` for example in terminal `SSH root@10.10.10.10` where `10.10.10.10` is the IP of your server

`Yes` to save your SSH key. If you are not asked for this, revisit your the setup page of your server.

Open the SSH config file with nano `/etc/ssh/sshd_config`

Change `#PasswordAuthentication yes` to `PasswordAuthentication no`. If you can't find this line, add it at the bottom of the file. This will disable user login via password only, instead requiring an SSH key.

`ctrl+x` to quit, `y` to save changes and then press `enter/return` to keep the same file name

Activate the changes with `service ssh reload`

Open the repository settings file with `nano /etc/apt/sources.list`

Add `deb http://http.us.debian.org/debian/ testing non-free contrib main` to the end of the file. This will allow the filesystem to be updated with the latest versions of software.

`ctrl+x` to quit, `y` to save changes and then press `enter/return` to keep the same file name

`apt-get update --allow-releaseinfo-change` to update the repository list in the OS

`apt-get upgrade` to install the latest updates. This may take a few minutes.

Install a popular intrusion prevention software such as `apt-get install fail2ban`. More information on how to configure this to your needs can be found here: `https://linuxize.com/post/install-configure-fail2ban-on-debian-10/`

Install the git framework, for downloading from GitHub with `apt-get install git`

Install the terminal multiplexer (alternative to screen) with `apt-get install tmux`

Install the session manager for tmux with `apt-get install tmuxp`

Make sure the system clock is always accurate with `apt-get install systemd-timesyncd`

Install the Python package installer with `apt-get install python3-pip` which will be used to install the libraries required for most python programs

## Session management

Sessions can be managed through multiple `screen`s however we have already installed the session manager `tmuxp` that allows the creation and termination of sessions with `tmux` easily.

Create the file session YAML file with `nano session.yaml`. Copy the code below with `ctrl + c` and paste it into the terminal window by `ctrl + v` or right click.

```
session_name: passiv_session
windows:
- window_name: passiv_1
  layout: even-vertical
  shell_command_before:
    - cd ~/passivbot
  panes:
    - shell_command:
        - python3 passivbot.py binance_01 COINUSDT configs/live/COIN.json -m future -lw 0.3
    - shell_command:
        -
    - shell_command:
        -
    - shell_command:
        -
- window_name: passiv_2
  layout: even-vertical
  shell_command_before:
    - cd ~/passivbot
  panes:
    - shell_command:
        -
    - shell_command:
        -
    - shell_command:
        -
    - shell_command:
        -
- window_name: passiv_3
  layout: even-vertical
  shell_command_before:
    - cd ~/passivbot
  panes:
    - shell_command:
        -
    - shell_command:
        -
    - shell_command:
        -
    - shell_command:
        -
- window_name: passiv_GS
  layout: even-vertical
  shell_command_before:
    - cd ~/passivbot
  panes:
    - shell_command:
        -
    - shell_command:
        - 
    - shell_command:
        - 
    - shell_command:
        - 
```

In this file there are four windows: `passiv_1`, `passiv_2`, `passiv_3` and `passiv_GS`

Each window has four slots for bot commands to run indicated under `- shell_command` with a `-`, the first slot has an example filled in for you. This setup allows for 16 bots to be run simultaneously however on closer inspection there is one window called `passiv_GS` for when you wish to use the `-gs` command (graceful stop) and keep other bots running

`ctrl+b` and then `w` - select a window to view

`ctrl+b` and then `d` - detach screen

`ctrl+b` and then `o` - move between the slots on each window

`ctrl+b` and then `s` - display all sessions

`ctrl+b` and then `{` or `}` - reorder slots on a window

`tmuxp load session.yaml` - load the screen manager (if they are already present, view the screen)

`tmux kill-session` - stop all bots/screens that are open (in case you need to reload the bot or change a configuration)

## Server rebooting
If your VPS reboots itself for whatever reason, you will want your bot to restart ASAP to ensure that orders and positions are maintained correctly. Further you will probably want to know and potentially investigate any issues.

Firstly you need to decide whether you want a notification to be sent to you and via which medium. The code below offers both discord and telegram notifications. If you need to use telegram, make sure you install the required library with the command `pip3 install telegram`. Make sure you change the `WEBHOOK_URL` if you're using discord and the `TELEGRAM_HTTP_API` and `TELEGRAM_USER_ID` if you're using telegram. It should go without saying that you want these to be sent privately, so don't use an open discord server or telegram group.

```
message = ":white_check_mark: VPS and passivbot rebooted"

def send_to_discord(message):
    import requests

    mUrl = "WEBHOOK_URL"

    data = {"content": message}
    response = requests.post(mUrl, json=data)

def send_to_telegram(message):
    import telegram

    telegram_http_api = "TELEGRAM_HTTP_API"
    telegram_user_id = "TELEGRAM_USER_ID"

    bot_binance = telegram.Bot(token=telegram_http_api)
    bot_binance.send_message(chat_id=telegram_user_id, text=message, parse_mode='HTML')

send_to_discord(message)
send_to_telegram(message)
```

You can test that it works by typing `python3 notify.py`.

There are two files required. One to start (reboot server) and one to restart (if you want it periodically to check it's alive). You can remove the notify.py from either or both files if you don't intend to use it

```
`#!/bin/bash

python3 notify.py
sleep 0.5
tmuxp load session.yaml
```

```
#!/bin/bash

python3 notify.py
sleep 0.5
tmux kill-session
sleep 0.5
tmuxp load session.yaml
```

These can be tested by typing `sh restart.sh `(if the session is running) and `sh start.sh` if not. If you are using `notify.py` you will receive a message unless you removed the code previously.

Next make sure the files sh files are executable by cron with `chmod +x start.sh restart.sh`

We're now ready to have these run automatically which can be done with crontab. To access this, type: `crontab -e` and selecting `1` to use nano as your editor.

At the bottom of the file, put the following two lines:

```
@reboot sleep 10; /bin/bash -c /root/start.sh
@hourly /bin/bash -c /root/restart.sh
```

`ctrl+x` to quit, `y` to save changes and then press `enter/return` to keep the same file name

Providing this is correct, you should see the message `crontab: installing new crontab` and then messages every hour or on a reboot.
<<<<<<< HEAD
# Smarter Digital Frame .bashrc

# Sourcing system bashrc if it exists
if [ -f /etc/bash.bashrc ]; then
    . /etc/bash.bashrc
fi

# User-specific aliases and functions
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'
alias frame-logs='tail -f logs/digitalframe.log'
alias frame-restart='sudo systemctl restart frame'
alias frame-status='sudo systemctl status frame'

# Custom prompt
export PS1="\[\e[32m\]frame-shell\[\e[m\]:\[\e[34m\]\w\[\e[m\]\$ "

# Ensure we are in the project root
cd /home/ram/photos/test/Smarterdigitalframe
=======
echo 'hi'
>>>>>>> 94e57d97e568581996585d058905c4ede42d527b

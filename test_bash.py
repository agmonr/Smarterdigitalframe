import pexpect
child = pexpect.spawn('/bin/bash', encoding='utf-8')
child.sendline('echo "hello"')
print(child.expect('hello'))

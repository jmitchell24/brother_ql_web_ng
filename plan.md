want a web interface for my brother label maker 

confirmed that it works with brother_ql. 

found this project: 
https://github.com/pklaus/brother_ql_web

its out of date. 

plan is to use this as a base, and expand it into what i want. 

first goal is to get this working. 

the we will work towards adding the things that i want. 


### this command worked

brother_ql -m QL-700 -b pyusb -p usb://0x04f9:0x2042 print -l 29x90 dk1201-label.png
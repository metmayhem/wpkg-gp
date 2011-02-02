# A Demo of services and named pipes.

# A multi-threaded service that executes WPKG.js and prompts
# returns it's output via a named pipe

import win32serviceutil, win32service
import pywintypes, win32con, winerror
from win32event import *
from win32file import *
from win32pipe import *
from win32api import *
from ntsecuritycon import *
import traceback
import thread
import servicemanager
import WPKGExecuter
import WpkgLGPUpdater
import _winreg, logging, logging.handlers
import os.path, shutil

MY_PIPE_NAME = r"\\.\pipe\WPKG"

def ApplyIgnoreError(fn, args):
    try:
        return fn(*args)
    except error: # Ignore win32api errors.
        return None
        

class WPKGControlService(win32serviceutil.ServiceFramework):
    _svc_name_ = "WpkgServer"
    _svc_display_name_ = "WPKG Control Service"
    _svc_description_ = "Controller service for userspace WPKG management applications. (http://wpkg-gp.googlecode.com/)"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = CreateEvent(None, 0, 0, None)
        self.overlapped = pywintypes.OVERLAPPED()
        self.overlapped.hEvent = CreateEvent(None,0,0,None)
        self.thread_handles = []
        self.WPKGExecuter = WPKGExecuter.WPKGExecuter()
        try:
            with _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, r"Software\policies\WPKG_GP") as key:
                verbosity = int(_winreg.QueryValueEx(key, "WpkgVerbosity")[0])
        except WindowsError:
            verbosity = 1
        if verbosity == 3:
            log_level = logging.DEBUG
        elif verbosity == 2:
            log_level = logging.INFO
        elif verbosity == 1:
            log_level = logging.ERROR
        else:
            log_level = logging.CRITICAL
        self.logger = logging.getLogger("WpkgService")
        logdir = os.path.join(os.path.dirname(__file__), "logs")
        try:
            os.makedirs(logdir)
        except WindowsError:
            pass
        logfile = os.path.join(logdir, "WpkgServiceLog")
        self.logger.setLevel(log_level)
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler = logging.handlers.RotatingFileHandler(logfile, maxBytes=200000, backupCount="2")
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.debug("Logging started")
        
    
    def CreatePipeSecurityObject(self):
        # Create a security object giving World read/write access,
        # but only "Owner" modify access.
        sa = pywintypes.SECURITY_ATTRIBUTES()
        sidEveryone = pywintypes.SID()
        sidEveryone.Initialize(SECURITY_WORLD_SID_AUTHORITY,1)
        sidEveryone.SetSubAuthority(0, SECURITY_WORLD_RID)
        sidCreator = pywintypes.SID()
        sidCreator.Initialize(SECURITY_CREATOR_SID_AUTHORITY,1)
        sidCreator.SetSubAuthority(0, SECURITY_CREATOR_OWNER_RID)

        acl = pywintypes.ACL()
        acl.AddAccessAllowedAce(FILE_GENERIC_READ|FILE_GENERIC_WRITE, sidEveryone)
        acl.AddAccessAllowedAce(FILE_ALL_ACCESS, sidCreator)

        sa.SetSecurityDescriptorDacl(1, acl, 0)
        return sa

    # The functions executed in their own thread to process a client request.
    def DoProcessClient(self, pipeHandle, tid):
        self.logger.debug("DoProcessClient() start")
        try:
            try:
                # Create a loop, reading large data.  If we knew the data stream was
                # was small, a simple ReadFile would do.
                d = ''.encode('ascii') # ensure bytes on py2k and py3k...
                hr = winerror.ERROR_MORE_DATA
                while hr==winerror.ERROR_MORE_DATA:
                    hr, thisd = ReadFile(pipeHandle, 256)
                    d = d + thisd
                    d = d.rstrip("\0") #remove trailing nulls
                ok = 1
            except error:
                self.logger.info("Client disconnected")
                # Client disconnection - do nothing
                ok = 0

            # A secure service would handle (and ignore!) errors writing to the
            # pipe
            if ok:
                if self.WPKGExecuter.getStatus() != "OK":
                    msg = "200 " + self.WPKGExecuter.getStatus()
                    self.logger.info("Wpkg Executer is not ready. Returning '%s' to client." % msg)
                    servicemanager.LogErrorMsg(msg)
                    WriteFile(pipeHandle, msg.encode('ascii'))
                else:	
                    if d == b"Execute":
                        self.logger.info("Received 'Execute', executing WPKG")
                        self.WPKGExecuter.Execute(pipeHandle, useWriteFile=True)
                    elif d == b"Cancel":
                        self.logger.info("Received 'Cancel', cancelling WPKG")
                        self.WPKGExecuter.Cancel(pipeHandle, useWriteFile=True)
                    elif d.split(" ")[0] == b"SetNetworkUser":
                        self.logger.info("Received 'SetNetworkUser', configuring the network user")
                        try:
                            username = d.split(" ")[1]
                            password = d.split(" ")[2]
                            self.WPKGExecuter.SetNetworkUser(username, password)
                            msg = "100 Successfully updated NetworkUser"
                            self.logger.info("Sending '%s' to client" % msg)
                            WriteFile(pipeHandle, msg.encode('ascii'))
                        except Exception as e:
                            msg = "200 Error when updating NetworkUser: %s" % e
                            self.logger.info("Sending '%s' to client" % msg)
                            WriteFile(pipeHandle, msg.encode('ascii'))
                            raise
                    elif d.split(" ")[0] == b"SetExecuteUser":
                        try:
                            username = d.split(" ")[1]
                            password = d.split(" ")[2]
                            self.WPKGExecuter.SetExecuteUser(username, password)
                            msg = "100 Successfully updated ExecuteUser"
                            self.logger.info("Sending '%s' to client" % msg)
                            WriteFile(pipeHandle, msg.encode('ascii'))
                        except Exception as e:
                            msg = "200 Error when updating ExecuteUser: %s" % e
                            self.logger.info("Sending '%s' to client" % msg)
                            WriteFile(pipeHandle, msg.encode('ascii'))
                            raise
                    elif d.split(" ")[0] == b"EnableViaLGP":
                        try:
                            action = d.split(" ")[1]
                            wpkggp = WpkgLGPUpdater.WpkgLocalGPConfigurator()
                            if action == "add":
                                wpkggp.addToLocalPolicies()
                                msg = "100 Successfully added to LGP"
                                self.logger.info("Sending '%s' to client" % msg)
                                WriteFile(pipeHandle, msg.encode('ascii'))
                            elif action == "remove":
                                wpkggp.removeFromLocalPolicies()
                                msg = "100 Successfully removed from LGP"
                                self.logger.info("Sending '%s' to client" % msg)
                                WriteFile(pipeHandle, msg.encode('ascii'))
                            else:
                                msg = "200 Error when trying to EnableViaLGP %s" % e
                                self.logger.info("Sending '%s' to client" % msg)
                                WriteFile(pipeHandle, msg.encode('ascii'))
                        except IndexError:
                            msg = "200 You need to run EnableViaLGP add|remove"
                            self.logger.info("Sending '%s' to client" % msg)
                            WriteFile(pipeHandle, msg.encode('ascii'))
                            raise
                        except Exception as e:
                            msg = "200 Error when trying to EnableViaLGP %s" % e
                            self.logger.info("Sending '%s' to client" % msg)
                            WriteFile(pipeHandle, msg.encode('ascii'))
                            raise
                        
                    else:
                        msg = "203 Unknown command: %s" % d
                        self.logger.info("Sending '%s' to client" % msg)
                        WriteFile(pipeHandle, msg.encode('ascii'))

                #msg = ("%s (on thread %d) sent me %s" % (GetNamedPipeHandleState(pipeHandle)[4],tid, d)).encode('ascii')
                #WriteFile(pipeHandle, msg)
        except Exception, e:
            self.logger.exception("Error when processing Named Pipe Client:")
            raise
        finally:
            ApplyIgnoreError( DisconnectNamedPipe, (pipeHandle,) )
            ApplyIgnoreError( CloseHandle, (pipeHandle,) )

    def ProcessClient(self, pipeHandle):
        try:
            procHandle = GetCurrentProcess()
            th = DuplicateHandle(procHandle, GetCurrentThread(), procHandle, 0, 0, win32con.DUPLICATE_SAME_ACCESS)
            try:
                self.thread_handles.append(th)
                try:
                    return self.DoProcessClient(pipeHandle, th)
                except:
                    traceback.print_exc()
            finally:
                self.thread_handles.remove(th)
        except:
            traceback.print_exc()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        # Write an event log record - in debug mode we will also
        # see this message printed.
        servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, '')
                )

        num_connections = 0
        #Waiting for an event
        while 1:
            pipeHandle = CreateNamedPipe(MY_PIPE_NAME,
                    PIPE_ACCESS_DUPLEX| FILE_FLAG_OVERLAPPED,
                    PIPE_TYPE_MESSAGE | PIPE_READMODE_BYTE,
                    PIPE_UNLIMITED_INSTANCES,       # max instances
                    0, 0, 6000,
                    self.CreatePipeSecurityObject())
            try:
                hr = ConnectNamedPipe(pipeHandle, self.overlapped)
            except error as details:
                print("Error connecting pipe!", details)
                CloseHandle(pipeHandle)
                break
            if hr==winerror.ERROR_PIPE_CONNECTED:
                # Client is already connected - signal event
                SetEvent(self.overlapped.hEvent)
            rc = WaitForMultipleObjects((self.hWaitStop, self.overlapped.hEvent), 0, INFINITE)
            if rc==WAIT_OBJECT_0:
                # Stop event, exit loop
                break
            else:
                # Pipe event - spawn thread to deal with it.
                thread.start_new_thread(self.ProcessClient, (pipeHandle,))
                num_connections = num_connections + 1

        # Sleep to ensure that any new threads are in the list, and then
        # wait for all current threads to finish.
        # What is a better way?
        Sleep(500)
        while self.thread_handles:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, 5000)
            print("Waiting for %d threads to finish..." % (len(self.thread_handles)))
            WaitForMultipleObjects(self.thread_handles, 1, 3000)
        # Write another event log record.
        servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, " after processing %d connections" % (num_connections,))
                )


if __name__=='__main__':
    win32serviceutil.HandleCommandLine(WPKGControlService)

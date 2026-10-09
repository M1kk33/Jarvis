using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Jarvis.Desktop;

// The user launches the root. Enrol it before it can create any children.
// All children inherit ownership at creation, including WebView2 and nested model jobs.
internal sealed class Ownership
{
    private readonly nint handle;
    public Ownership()
    {
        handle = CreateJobObjectW(0, null);
        if (handle == 0) throw new Win32Exception();
        var limits = new ExtendedLimits();
        limits.Basic.Flags = 0x2000; // KILL_ON_JOB_CLOSE; never enable breakaway.
        if (!SetInformationJobObject(handle, 9, ref limits, Marshal.SizeOf<ExtendedLimits>())
            || !AssignProcessToJobObject(handle, GetCurrentProcess()))
            throw new Win32Exception();
        // Deliberately retain the non-inherited handle for the lifetime of this root.
        // Closing it here would terminate the root as well. OS closes it on exit/crash.
    }

    public uint ActiveProcesses
    {
        get
        {
            if (!QueryInformationJobObject(handle, 1, out Accounting value, Marshal.SizeOf<Accounting>(), 0))
                throw new Win32Exception();
            return value.Active;
        }
    }

    public bool Contains(nint process) => IsProcessInJob(process, handle, out bool present) && present;

    [StructLayout(LayoutKind.Sequential)] private struct BasicLimits
    {
        public long UserTime, JobTime;
        public uint Flags;
        public nuint Minimum, Maximum;
        public uint Active;
        public nuint Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits
    {
        public BasicLimits Basic;
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        public nuint ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Accounting
    {
        public long User, Kernel, PeriodUser, PeriodKernel;
        public uint Faults, Total, Active, Terminated;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern nint CreateJobObjectW(nint attributes, string? name);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(nint job, int kind, ref ExtendedLimits value, int length);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(nint job, nint process);
    [DllImport("kernel32.dll")] private static extern nint GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool QueryInformationJobObject(nint job, int kind, out Accounting value, int length, nint returned);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool IsProcessInJob(nint process, nint job, out bool present);
}

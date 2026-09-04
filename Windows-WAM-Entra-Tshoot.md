## WAM/Entra Token Troubleshooting for integrations that live completely on your LOCAL MACHINE

This applies to any locally-running app that uses WAM/MSAL broker auth against Office 365 — ResearchMesh-Windows and Microsoft's own 365 MCP server are just two examples that surfaced it.

<details>
<summary><b>Problem scenarios</b> — situations Microsoft's documentation, Microsoft Support, and your own Windows Enterprise Admins are all equally useless at helping you troubleshoot, because you don't yet know how to explain the problem</summary>

- Scenario Examples:
-- You can't get the full feature set in ResearchMesh-Windows to fully integrate with your Office 365 apps.
-- You can't get Microsoft's 365 MCP server that runs on your LOCAL MACHINE to integrate with your office 365 apps.
-- You have to login to the Microsoft Store with a different account than your Office 365 Account
-- The SSO domain accounts in the old school Control Panel's Credential Manager (under User Accounts) shows a different domain account than what you use to log into your Office 365 apps with
-- You can't get ANY local integrations that run completely on your LOCAL MACHINE to integrate with office 365 apps, even with PowerShell as your LOCAL user.
-- You have problems getting PowerShell to execute tasks for your Office 365 apps
-- You have MULTIPLE working and/or broken One Drive accounts and are concerned about losing your data if you try to fix your WAM/Entra authentication problems.
-- You bought a windows desktop, used your personal email address to create the login with for the SSO with the Microsoft Store. 6 months later, you started your own business, bought a domain from godaddy, and used your professional email account with your domain you purchased for your Office 365 domain cred access, and you get WAM/Entra token authentication failures even though in Settings -> Accounts -> Email and Accounts, it shows both accounts associated with your desktop.

- **Question:** Why is this such a problem?
- **Answer:** Because Microsoft's different teams for each product/service/feature/etc due to segregation/separation of duties are not very good at converging in official documentation when it comes to trying to troubleshoot issues regarding WAM/Entra token usage. Another reason they keep their documentation so vague is because if they list something as a hard limitation spec, they can face lawsuits over this, so it's cheaper to be vague in order to avoid lawsuits, leaving Microsoft Support with no guidance to help you fix your WAM/Entra token authentication problems based on your goal, not what vague documentation spec says is OK.

  Don't just take my word for the vagueness — go look at Microsoft's own dedicated WAM errors/mitigations page. Several of the handful of errors it does list have a completely blank "Mitigation" column, and the specific named error for the exact account-mismatch problem this whole doc is about isn't even in the table. Its own closing advice for anything not listed is to file a GitHub issue and wait: https://learn.microsoft.com/en-us/entra/msal/dotnet/advanced/exceptions/wam-errors

</details>

<details>
<summary><b>Review your system</b> — dsregcmd output, WAM state, and where to check for an SSO account mismatch</summary>

Run:

```bash
dsregcmd /status
```
Microsoft's own field-by-field reference for every value this command prints (what each one means, in isolation) is here — useful for looking up an individual field, but don't expect it to connect the dots for you on WHY a mismatch breaks anything, that's not what this page is for: https://learn.microsoft.com/en-us/entra/identity/devices/troubleshoot-device-dsregcmd

The interesting section is Device State. Log the data on something you can get to in case you can NOT get into your LOCAL MACHINE.
Maybe take a picture of it with your phone.

Look specifically at:

```bash
AzureAdJoined
EnterpriseJoined
DomainJoined
```

and then the SSO State section, particularly:

```bash
AzureAdPrt
AzureAdPrtUpdateTime
```

and then look for these

```bash
User State
    WorkplaceJoined : YES/NO
    WamDefaultSet   : YES/NO
    WamDefaultAuthority
    WamDefaultId
    WamDefaultGUID
```

If they ALL say NO or even just some of them, you can STILL HAVE YOUR WORKPLACE JOINED and use your Office365 apps normally,
and when you open the newer settings menu/panel from the windows menu, you might see 2 (or more) associated accounts here:

Windows Menu -> Accounts -> (scroll down to) Email and Accounts
(and don't get confused because the sub menu item has the word Email in it, just let that thought go!)

Notice the primary account shown over to the left of the list of associated accounts that you see on any windows setting. 
That is your LOCAL MACHINE DOMAIN SSO/ACCOUNT — the same one you'll find in the old school Control Panel's Credential Manager (User Accounts -> Credential Manager in Category view, or just "Credential Manager" directly in Large/Small icons view).
You will also see it in the list of your associated accounts, along with your 365 domain account if your 365 account happens to be different.

- **Question:** What does this mean if your LOCAL MACHINE SSO/ACCOUNT is DIFFERENT than your Office 365 account login?
- **Answer:** It means you might have WAM/Entra authentication problems when trying to setup integrations that run completely on your LOCAL MACHINE, especially MCP servers that run STDIO or streamableHTTP, that you want to connect to your office 365 apps with. It also means you will have to login to the Microsoft Store with a different login that may not be your Office 365 login.

  This isn't speculation — it's how Microsoft's own broker documentation says WAM works under the hood: local apps authenticate against "the account you signed into your Windows session," not whatever account you happen to be using in a browser tab. See Microsoft's own MSAL/WAM broker docs: https://learn.microsoft.com/en-us/entra/msal/dotnet/acquiring-tokens/desktop-mobile/wam

Now for something else to verify. Open the old school "Control Panel". If it's showing "Category" view, click on "User Accounts" (on older Windows 7/Vista-style builds this same tile is labeled "User Accounts and Family Safety") and then click "Credential Manager" inside it. If Control Panel is instead set to "Large icons" or "Small icons" view (the classic flat list, closer to how it looked in Windows XP), "Credential Manager" shows up directly as its own icon in that list — click it straight away, no "User Accounts" click needed first.
Either way you land on Credential Manager, and you will see "Web Credentials" and "Windows Credentials" as the two tabs. Click on "Windows Credentials".

You will see some blue highlighted text that looks like a link, and you will see "SSO" in the name.
See if the domain in the "SSO" is for your personal account, or if it is with your Microsoft Office 365 domain account.
If it is NOT the same as your Microsoft Office 365 account, it means you will have problems with local integrations on your LOCAL MACHINE that you setup through PowerShell that integrate with your Microsoft Office 365 account, including Microsoft's official 365 mcp server as well as features in this ResearchMesh-Windows project that use PowerShell.

  This exact mismatch has a real, named error inside Microsoft's own MSAL SDK — a developer hit it directly and filed it against Microsoft's own GitHub repo: "Wam returned default account that doesn't match the account passed in" (internal error code 508065737). So this isn't a theory, it's a documented failure mode: https://github.com/AzureAD/microsoft-authentication-library-for-dotnet/issues/4945

</details>

<details>
<summary><b>Before you touch your LOCAL MACHINE SSO creds</b> — back up OneDrive and your local files first</summary>

Its possible your One Drive settings may be set up to replicate anything you throw in your LOCAL MACHINE's "MyDocuments" folder into your OneDrive Cloud storage "MyDocuments" folder, and that maybe it never worked right in the first place. As soon as you swap your SSO domain creds for the LOCAL MACHINE to match your office 365 domain creds, your entire OneDrive might get overwritten, so as a safety precaution, get a detachable hard drive, and make a local backup of you entire OneDrive contents and ANY local files so you can mess with settings and accounts for the LOCAL MACHINE and not have to worry if something gets overwritten. Once you get your SSO domain creds, your office 365 cred and One Drive settings worked out, you can slowly start moving data back based on your One Drive settings you finalized on, just to test the waters and make sure the behavior is working as expected, which may not mean its the way you want, but the way it works based on how Microsoft built these products.

</details>

<details>
<summary><b>Fixing the mismatch</b> — aligning your LOCAL MACHINE SSO account with your Office 365 account</summary>

- Based upon the information in my prior argument about WAM/Entra token issues already discussed, check the old school Control Panel's Credential Manager (User Accounts -> Credential Manager) and see if the SSO domain account is the same as your Office 365 login domain account. If it matches, no fix is required here, and if you still have WAM/Entra token authentication issues, then your problems are elsewhere.
- If your SSO domain account in Credential Manager, under the Windows Credentials tab, does NOT match what you use to login to Office 365 with, back up all of your documents from your OneDrive and your LOCAL MACHINE's MyDocuments to an offline backup source or some other online backup source. If your offline or online backup source is set to automatic, you might want to disable that after you back everything up so that doesn't get overwritten, at least for this process.
- Add your office 365 domain account/creds to the Generic creds section so that you see BOTH the 365 creds AND the existing SSO creds that are different. Be sure you match everything. If you have 3 entries for your old SSO creds, then build the same 3 equivalent SSO cred types for your Office 365 domain creds.
- Reboot your computer, so the local machine authenticates on the newly added creds.
- Open the old school Control Panel's Credential Manager, under the Windows Credentials tab, and delete the old SSO creds that are NOT your office 365 domain creds you just entered before the reboot.
- Reboot the machine again. Now you should have ONE set of creds under the new Settings menu in the windows menu under the:  Accounts -> Email and Accounts (submenu), and that your icon on the left of the list of accounts shows your office 365 account as your LOCAL MACHINE account.
- At this point, NOW your local machine can authenticate integrations that run completely on your LOCAL MACHINE and powershell will be able to execute things in your office 365 apps now, and all of the features of ResearchMesh-Windows will work as expected.
- For those OneDrive users, NOW is your chance to truly review and clean up your OneDrive accounts, broken, working, extras accounts you don't need and start from scratch BEFORE you start moving your data back into your one drive. Review all your One Drive settings and make sure it works with a few test documents, such as creating a local text file and seeing if it ends up in your OneDrive folders as expected, based upon your corrected OneDrive settings, whatever it is you set them as for your purposes.

</details>

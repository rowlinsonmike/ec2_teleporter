<p align="center">
  <img width="800" height="200" src="img/logo.png">
</p>
<h4 align="center">EC2 Teleporter</h4>


## About
It is tedious to move EC2 instances around in the AWS environment. Many steps are involved and ensuring things like tags being applied to the new instance and volumes is error prone. Not to mention the extra layer of debauchery that takes place when encryption is involved. Enter `ec2_teleporter`✨🚀.

Designed for use with AWS...*obviously*, and Python 3.7. This tool currently supports `EBS backed` instances only. See the Features list below. 

## Installation
1. `git clone https://github.com/rowlinsonmike/ec2_teleporter`
2. `cd ./ec2_teleporter`
3. `pip install requirements.txt`

**Usage**
---

```
python ec2_teleporter.py
```

## Cofiguration
`ec2_teleporter` requires 2 profiles in your `~/.aws/credentials` file
1. One profile should have the name `src` and should contain access keys for source account
2. One profile should have the name `dst` and should contain access keys for destination account

## Run Steps
1. run `python ec2_teleporter.py`
2. select `source region` from given prompt
3. select `destination region` from given prompt
4. input `instance-id` when prompted
5. instance will be powered off
6. AMI will be created from instance `or` prompted to use existing AMI
7. select destination `vpc` from prompt
8. select destination `subnet` from prompt
9. select destination `security group` from prompt
10. select destination `instance profile` from prompt
11. If original instance is encrypted you will be prompted to select destination `kms` key to use. Else you will be asked whether you would like the instance encrypted or not.
12. Confirm all your selections with `y` or backout with `n`
13. Prompted to cleanup AMI that was created. `y` will delete AMI and snapshots.
14. Prompted to terminate original instance. `y` will delete original instance *even if termination protection is enabled*.
15. New `instance-id` will be displayed

## Features
|                            | Current Version  
| -------------------------- | :----------------:  
| teleport unencrypted/encrypted instance same region same account                             |        ✅
| teleport unencrypted/encrypted instance cross region same account                            |        ✅
| teleport unencrypted/encrypted instance same region cross account                            |        ✅
| teleport unencrypted/encrypted instance cross region cross account                           |        ✅
| delete resources (AMIs,snapshots,instance) after teleport                                    |        ✅
| Ability to teleport ephemeral instances                                                      |        ❌        
| Ability to teleport to a dedicated host                                                      |        ❌        
| Ability to teleport from AMI instead of instance                                             |        ❌    
| Ability to teleport a default encrypted instance                                             |        ❌ 
| Ability to use IAM roles instead of profiles                                                 |        ❌ 


## FAQs
1. Can I teleport a instance encrypted with default encryption? No. YOU MUST BE USING KMS CMKs in order to use this tool currently. 
2. Where can this process fail? It is possible that for various reasons the script times out waiting for either AMI creation or AMI copy. However, the waiters are set for 40 minutes, so if a timeout occurs something is likely wrong. 
3. What settings are applied on the new volumes? EBS volumes are set to delete with instance on termination when new instance is deployed and also recieve any tags the instance itself recieves.

## Pro Tips
1. Use temporary access keys. AWS SSO is worth setting up if you haven't.
2. If you already have an AMI of an instance you want to use, just make sure the AMI is named "Teleport-[instance-id]". `ec2-teleporter` will find it.

## How to Contribute
**Updates**
1. Clone repo and create a new branch: `$ git checkout https://github.com/rowlinsonmike/ec2_teleporter -b name_for_new_branch`.
2. Make changes and test
3. Submit Pull Request with comprehensive description of changes

**Issues**
1. Submit an issue with details that include logs and how one would emulate.

## Support
Reach out to me at one of the following places:

- website: [mikerowlinson.com](https://mikerowlinson.com)
- email: rowlinsonmike@gmail.com


## License
[WTFPL](www.wtfpl.net)
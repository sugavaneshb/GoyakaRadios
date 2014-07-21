A google app engine app [Goyaka Radios](goyakaradios.appspot.com) to curate playlists based on videos
added in the facebook group [GoyakaRadios](https://www.facebook.com/groups/goyakaradios/)

To do list:

1. Solve eventual persistant issues with datastore (put())
2. Have a landing page for the app
3. Admin view in the app to view the stats

Feature Requests:

1. Index and create playlists taking into account number of likes
2. Language specific playlists


To clone this for other facebook groups or add features: 

1. Clone this repo.
2. Get client secrets file using 'web application' as type of the application 
... [Don't forget to give correct redirect URI 's]
3. Replace `config/default_fb_properties.cfg` with `fb_properties.cfg` updating the values in the config file
4. Tweak the cron file to your requirements. 
5. Run init datastores first to initialize the datastores.
6. Do a manual fetch `/fetch` firsttime to help your app get the credentials to be used for future purposes

Credits: [DailyGrooves](https://github.com/ronjouch/dailygrooves), [FBGroupArchiver] (https://github.com/sugavaneshb/FBGroupArchiver), StackOverflow and GoogleAppEngine Developer docs 